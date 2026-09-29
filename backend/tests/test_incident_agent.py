import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings
from app.database import Base, get_db
from app.models import Incident
from app.routes import router
from app.services.hindsight import HindsightError, get_hindsight_adapter
from app.memory_routes import hindsight_error_handler


class IncidentInvestigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.calls: list[tuple[str, object]] = []
        self.fixture_memory = {
            "id": "memory-17",
            "type": "experience",
            "text": "A prior payment latency spike followed a deploy; reducing the connection pool queue fixed it.",
            "context": "Resolved payments-api incident",
        }
        self.fixture_reflection = "Earlier payment latency after deployment tracked with connection-pool wait saturation."
        self.retained_memories: list[dict[str, str]] = []

        class FakeHindsight:
            async def recall(inner_self, **kwargs):
                self.calls.append(("recall", kwargs))
                return {"results": [self.fixture_memory, *self.retained_memories]}

            async def reflect(inner_self, **kwargs):
                self.calls.append(("reflect", kwargs))
                retained_summary = " ".join(memory["text"] for memory in self.retained_memories)
                reflection = f"{self.fixture_reflection} Previous resolution: {retained_summary}" if retained_summary else self.fixture_reflection
                return {"text": reflection}

            async def retain(inner_self, **kwargs):
                self.calls.append(("retain", kwargs))
                if getattr(self, "retain_error", False):
                    raise HindsightError("The Hindsight service could not be reached.", status_code=502, code="hindsight_unavailable")
                self.retained_memories.append({
                    "id": f"retained-{len(self.retained_memories) + 1}",
                    "type": "experience",
                    "text": kwargs["content"],
                    "context": kwargs.get("context", ""),
                })
                return {"success": True}

        def db_session():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        self.app = FastAPI()
        self.app.include_router(router)
        self.app.add_exception_handler(HindsightError, hindsight_error_handler)
        self.app.dependency_overrides[get_db] = db_session
        self.app.dependency_overrides[get_hindsight_adapter] = lambda: FakeHindsight()
        self.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None, openai_api_key="test-key")
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _create_incident(self) -> int:
        response = self.client.post("/api/incidents", json={"title": "Payment API latency spike", "service": "payments-api"})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_investigation_recalls_then_generates_json_recommendation(self) -> None:
        incident_id = self._create_incident()
        model_output = json.dumps({
            "learned_pattern": "A previous payment latency spike followed a deployment and improved after queue pressure was reduced.",
            "recommendation": "Check whether a recent deploy changed connection-pool usage, then inspect pool wait time and queue depth. If saturation began with the deploy, roll it back and verify p95 latency recovers.",
        })
        generated: list[dict] = []

        async def generate(**kwargs):
            self.calls.append(("generate", kwargs))
            generated.append(kwargs)
            return model_output

        with patch("app.services.incident_agent.generate_chat_response", side_effect=generate):
            response = self.client.post(
                f"/api/incidents/{incident_id}/investigate",
                json={"evidence": "p95 latency increased after the latest deployment."},
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["incident"]["title"], "Payment API latency spike")
        self.assertEqual(body["recalled_memories"][0]["text"], self.fixture_memory["text"])
        self.assertEqual(body["learned_pattern"], json.loads(model_output)["learned_pattern"])
        self.assertIn("connection-pool", body["recommendation"])
        self.assertEqual([name for name, _ in self.calls], ["recall", "reflect", "generate"])
        self.assertIn("payments-api", self.calls[0][1]["query"])
        self.assertIn("latest deployment", self.calls[0][1]["query"])
        self.assertEqual(generated[0]["memories"][0]["text"], self.fixture_memory["text"])
        self.assertEqual(generated[0]["memories"][0]["context"], self.fixture_memory["context"])
        self.assertEqual(generated[0]["reflection"], self.fixture_reflection)

    def test_investigation_uses_hindsight_fallback_without_llm_key(self) -> None:
        incident_id = self._create_incident()
        self.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)

        with patch("app.services.incident_agent.generate_chat_response", side_effect=AssertionError("LLM must not be called")):
            response = self.client.post(
                f"/api/incidents/{incident_id}/investigate",
                json={"evidence": "p95 latency increased after the latest deployment."},
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["recalled_memories"][0]["text"], self.fixture_memory["text"])
        self.assertEqual(body["learned_pattern"], self.fixture_reflection)
        self.assertIn("Hindsight Reflect:", body["recommendation"])
        self.assertIn("p95 latency increased", body["recommendation"])
        self.assertIn("payment-provider health", body["recommendation"])
        self.assertIn("rollback is safe", body["recommendation"])
        self.assertIn("prior payment latency", body["recommendation"])
        self.assertEqual([name for name, _ in self.calls], ["recall", "reflect"])

    def test_investigation_returns_not_found_for_unknown_incident(self) -> None:
        response = self.client.post("/api/incidents/999/investigate", json={})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.calls, [])

    def test_resolve_retains_outcome_and_updates_only_incident_metadata(self) -> None:
        incident_id = self._create_incident()
        self.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
        outcome = "Rolled back the latest payment API deployment and latency returned to normal."
        evidence = "p95 latency increased after the latest deployment."

        response = self.client.post(
            f"/api/incidents/{incident_id}/resolve",
            json={"outcome": outcome, "evidence": evidence},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertTrue(body["retained"])
        self.assertEqual(body["outcome"], outcome)
        self.assertEqual(body["incident"]["status"], "resolved")
        self.assertEqual([name for name, _ in self.calls], ["retain"])
        retained = self.calls[0][1]
        self.assertIn("Payment API latency spike", retained["content"])
        self.assertIn("payments-api", retained["content"])
        self.assertIn(evidence, retained["content"])
        self.assertIn(outcome, retained["content"])
        self.assertIn("resolved", retained["content"])
        self.assertIsNotNone(retained["timestamp"])
        self.assertEqual(retained["document_id"], f"incident-{incident_id}")

        with self.session_factory() as session:
            incident = session.get(Incident, incident_id)
            self.assertEqual(incident.status, "resolved")
            self.assertFalse(hasattr(incident, "recalled_memories"))

    def test_resolve_does_not_mark_incident_resolved_if_retain_fails(self) -> None:
        incident_id = self._create_incident()
        self.retain_error = True

        response = self.client.post(
            f"/api/incidents/{incident_id}/resolve",
            json={"outcome": "Rolled back the deployment successfully.", "evidence": "Latency spiked after deploy."},
        )

        self.assertEqual(response.status_code, 502, response.text)
        with self.session_factory() as session:
            incident = session.get(Incident, incident_id)
            self.assertEqual(incident.status, "open")

    def test_second_incident_is_new_and_uses_retained_hindsight_outcome(self) -> None:
        self.app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
        first_id = self._create_incident()
        first_evidence = "p95 latency increased after the latest deployment."
        outcome = "Rolled back the latest payment API deployment and latency returned to normal."

        first_investigation = self.client.post(
            f"/api/incidents/{first_id}/investigate",
            json={"evidence": first_evidence},
        )
        self.assertEqual(first_investigation.status_code, 200, first_investigation.text)
        first_resolve = self.client.post(
            f"/api/incidents/{first_id}/resolve",
            json={"outcome": outcome, "evidence": first_evidence},
        )
        self.assertEqual(first_resolve.status_code, 200, first_resolve.text)
        self.assertTrue(first_resolve.json()["retained"])

        second_create = self.client.post(
            "/api/incidents",
            json={"title": "Payment API latency spike again", "service": "payments-api"},
        )
        self.assertEqual(second_create.status_code, 201, second_create.text)
        second_id = second_create.json()["id"]
        self.assertNotEqual(first_id, second_id)
        second_evidence = "p95 latency increased again after a recent deployment."
        second_investigation = self.client.post(
            f"/api/incidents/{second_id}/investigate",
            json={"evidence": second_evidence},
        )

        self.assertEqual(second_investigation.status_code, 200, second_investigation.text)
        body = second_investigation.json()
        self.assertEqual(body["incident"]["id"], second_id)
        self.assertEqual(body["incident"]["status"], "open")
        self.assertTrue(any(outcome in memory["text"] for memory in body["recalled_memories"]))
        self.assertIn(outcome, body["learned_pattern"])
        self.assertIn(outcome, body["recommendation"])
        self.assertEqual([name for name, _ in self.calls], ["recall", "reflect", "retain", "recall", "reflect"])
        self.assertIn("Payment API latency spike again", self.calls[-2][1]["query"])
        self.assertIn(second_evidence, self.calls[-2][1]["query"])

        with self.session_factory() as session:
            rows = session.query(Incident).order_by(Incident.id).all()
            self.assertEqual(len(rows), 2)
            self.assertEqual([row.id for row in rows], [first_id, second_id])
            self.assertEqual(rows[0].status, "resolved")
            self.assertEqual(rows[1].status, "open")


if __name__ == "__main__":
    unittest.main()
