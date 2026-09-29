import json
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat_routes import chat_router
from app.config import Settings, get_settings
from app.services.hindsight import get_hindsight_adapter
from app.services.llm import generate_chat_response


class ChatRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, object]] = []

        class FakeHindsight:
            async def recall(inner_self, **kwargs):
                self.calls.append(("recall", kwargs))
                return {"results": [{"text": "The user is building an ecommerce project.", "type": "experience"}]}

            async def retain(inner_self, **kwargs):
                self.calls.append(("retain", kwargs))
                return {"success": True}

        self.app = FastAPI()
        self.app.include_router(chat_router)
        self.app.dependency_overrides[get_hindsight_adapter] = lambda: FakeHindsight()
        self.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, openai_api_key="test-key")
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_message_recalls_generates_then_retains_durable_fact(self) -> None:
        async def fake_generate(**kwargs):
            self.calls.append(("generate", kwargs))
            return "Sure! Here are some ecommerce project ideas: a personalized product finder, subscription bundles, and a sustainable resale marketplace."

        with patch("app.chat_routes.generate_chat_response", side_effect=fake_generate):
            response = self.client.post(
                "/api/chat",
                json={"message": "I'm working on an ecommerce project. Tell me ideas."},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("Here are some ecommerce project ideas", body["answer"])
        self.assertTrue(body["retained"])
        self.assertEqual([name for name, _ in self.calls], ["recall", "generate", "retain"])
        generated = self.calls[1][1]
        self.assertEqual(generated["message"], "I'm working on an ecommerce project. Tell me ideas.")
        self.assertEqual(generated["memories"][0]["text"], "The user is building an ecommerce project.")
        self.assertEqual(self.calls[2][1]["content"], "I'm working on an ecommerce project.")

    def test_simple_question_is_not_retained_as_a_memory(self) -> None:
        async def fake_generate(**kwargs):
            self.calls.append(("generate", kwargs))
            return "You're working on an ecommerce project."

        with patch("app.chat_routes.generate_chat_response", side_effect=fake_generate):
            response = self.client.post("/api/chat", json={"message": "What project am I working on?"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "You're working on an ecommerce project.")
        self.assertFalse(response.json()["retained"])
        self.assertEqual([name for name, _ in self.calls], ["recall", "generate"])


class OpenAIResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def test_responses_api_output_is_used_as_chat_answer(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "A real model answer."}]}]})

        answer = await generate_chat_response(
            settings=Settings(_env_file=None, openai_api_key="test-key", openai_model="test-model"),
            message="Tell me some ecommerce ideas.",
            history=[{"role": "user", "content": "I'm working on an ecommerce project."}],
            memories=[{"text": "The user is building an ecommerce project."}],
            transport=httpx.MockTransport(handle),
        )

        self.assertEqual(answer, "A real model answer.")
        self.assertEqual(str(requests[0].url), "https://api.openai.com/v1/responses")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["model"], "test-model")
        self.assertFalse(payload["store"])
        self.assertIn("The user is building an ecommerce project.", payload["instructions"])
        self.assertEqual(payload["input"][-1], {"role": "user", "content": "Tell me some ecommerce ideas."})


if __name__ == "__main__":
    unittest.main()
