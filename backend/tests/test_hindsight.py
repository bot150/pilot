import json
import unittest

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.memory_routes import hindsight_error_handler, memory_router
from app.services.hindsight import HindsightAdapter, HindsightError, get_hindsight_adapter


def configured_settings(**overrides) -> Settings:
    values = {
        "hindsight_api_url": "https://hindsight.example.test",
        "hindsight_api_key": "test-secret-key",
        "hindsight_bank_id": "incidentpilot-test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class HindsightAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_retain_many_posts_document_chunks_together(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True, "items_count": 2})

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        result = await adapter.retain_many([
            {"content": "first page", "context": "guide.pdf", "document_id": "doc-1"},
            {"content": "second page", "context": "guide.pdf", "document_id": "doc-1"},
        ])

        self.assertEqual(result["items_count"], 2)
        self.assertEqual(len(requests), 1)
        payload = json.loads(requests[0].content)
        self.assertFalse(payload["async"])
        self.assertEqual(len(payload["items"]), 2)
        self.assertTrue(all(item["document_id"] == "doc-1" for item in payload["items"]))

    async def test_retain_posts_document_and_context_to_bank(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True, "items_count": 1})

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        result = await adapter.retain(
            content="PostgreSQL connections were exhausted after deployment.",
            context="production incident",
            document_id="incident-42",
            timestamp="2026-09-28T12:00:00+00:00",
        )

        self.assertEqual(result["items_count"], 1)
        self.assertEqual(
            str(requests[0].url),
            "https://hindsight.example.test/v1/default/banks/incidentpilot-test/memories",
        )
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-secret-key")
        self.assertEqual(
            json.loads(requests[0].content),
            {
                "items": [
                    {
                        "content": "PostgreSQL connections were exhausted after deployment.",
                        "context": "production incident",
                        "document_id": "incident-42",
                        "timestamp": "2026-09-28T12:00:00+00:00",
                    }
                ],
                "async": False,
            },
        )

    async def test_recall_posts_query_and_filters(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"results": [{"text": "Pool exhaustion"}]})

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        result = await adapter.recall(
            query="Have we seen payment latency after deploys?",
            types=["experience", "observation"],
            budget="high",
            max_tokens=2048,
        )

        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(str(requests[0].url).endswith("/memories/recall"))
        self.assertEqual(
            json.loads(requests[0].content),
            {
                "query": "Have we seen payment latency after deploys?",
                "types": ["experience", "observation"],
                "budget": "high",
                "max_tokens": 2048,
            },
        )

    async def test_reflect_requests_answer_with_supporting_facts(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"text": "Check the connection pool.", "based_on": {}})

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        result = await adapter.reflect(query="What should we investigate first?")

        self.assertIn("text", result)
        self.assertTrue(str(requests[0].url).endswith("/reflect"))
        self.assertEqual(
            json.loads(requests[0].content),
            {
                "query": "What should we investigate first?",
                "budget": "low",
                "max_tokens": 4096,
                "include": {"facts": {}},
            },
        )

    async def test_missing_configuration_names_required_variables(self) -> None:
        adapter = HindsightAdapter(Settings(_env_file=None))
        with self.assertRaises(HindsightError) as caught:
            await adapter.recall(query="anything")
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn("HINDSIGHT_API_KEY", caught.exception.message)
        self.assertNotIn("test-secret-key", caught.exception.message)

    async def test_connection_failure_is_sanitized(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("socket detail containing secret")

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        with self.assertRaises(HindsightError) as caught:
            await adapter.retain(content="example")
        self.assertEqual(caught.exception.code, "hindsight_unavailable")
        self.assertNotIn("socket detail", caught.exception.message)

    async def test_timeout_maps_to_gateway_timeout(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("internal timeout detail")

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        with self.assertRaises(HindsightError) as caught:
            await adapter.recall(query="anything")
        self.assertEqual(caught.exception.status_code, 504)
        self.assertEqual(caught.exception.code, "hindsight_timeout")
        self.assertNotIn("internal timeout", caught.exception.message)

    async def test_upstream_auth_error_is_sanitized(self) -> None:
        def handle(_: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="provider response that may contain details")

        adapter = HindsightAdapter(
            configured_settings(), transport=httpx.MockTransport(handle)
        )
        with self.assertRaises(HindsightError) as caught:
            await adapter.recall(query="anything")
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("Check the API key and bank access", caught.exception.message)
        self.assertNotIn("provider response", caught.exception.message)


class MemoryEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[tuple[str, dict]] = []

        class FakeAdapter:
            async def retain(inner_self, **kwargs):
                self.calls.append(("retain", kwargs))
                return {"success": True, "items_count": 1}

            async def recall(inner_self, **kwargs):
                self.calls.append(("recall", kwargs))
                return {"results": [{"text": "historical evidence"}]}

            async def reflect(inner_self, **kwargs):
                self.calls.append(("reflect", kwargs))
                return {"text": "investigate pool saturation", "based_on": {}}

        self.fake_adapter = FakeAdapter()
        self.app = FastAPI()
        self.app.include_router(memory_router)
        self.app.add_exception_handler(HindsightError, hindsight_error_handler)
        self.app.dependency_overrides[get_hindsight_adapter] = lambda: self.fake_adapter
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()

    def test_retain_recall_and_reflect_endpoints_call_adapter(self) -> None:
        retained = self.client.post(
            "/api/memory/retain",
            json={"content": "incident details", "document_id": "inc-1"},
        )
        recalled = self.client.post(
            "/api/memory/recall", json={"query": "similar incident"}
        )
        reflected = self.client.post(
            "/api/memory/reflect", json={"query": "what should we try?"}
        )

        self.assertEqual(retained.status_code, 200)
        self.assertEqual(recalled.status_code, 200)
        self.assertEqual(reflected.status_code, 200)
        self.assertEqual([operation for operation, _ in self.calls], ["retain", "recall", "reflect"])
        self.assertEqual(self.calls[0][1]["document_id"], "inc-1")

    def test_missing_configuration_returns_clear_503(self) -> None:
        self.app.dependency_overrides[get_hindsight_adapter] = lambda: HindsightAdapter(
            Settings(_env_file=None)
        )
        response = self.client.post("/api/memory/recall", json={"query": "anything"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "hindsight_not_configured")
        self.assertIn("HINDSIGHT_BANK_ID", response.json()["detail"]["message"])


if __name__ == "__main__":
    unittest.main()
