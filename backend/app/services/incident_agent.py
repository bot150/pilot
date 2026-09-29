from __future__ import annotations

import json
import re
from typing import Any

from app.chat_routes import _memory_items
from app.config import Settings
from app.models import Incident
from app.schemas import IncidentInvestigationResponse, IncidentRead
from app.services.hindsight import HindsightAdapter, HindsightError
from app.services.llm import LLMError, generate_chat_response


def _analysis_json(response: str) -> tuple[str | None, str]:
    candidate = response.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        result: Any = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise LLMError("The AI service returned an invalid incident analysis.", status_code=502) from exc
    if not isinstance(result, dict):
        raise LLMError("The AI service returned an invalid incident analysis.", status_code=502)

    recommendation = result.get("recommendation")
    learned_pattern = result.get("learned_pattern")
    if not isinstance(recommendation, str) or not recommendation.strip():
        raise LLMError("The AI service returned no incident recommendation.", status_code=502)
    if learned_pattern is not None and not isinstance(learned_pattern, str):
        raise LLMError("The AI service returned an invalid incident analysis.", status_code=502)
    return (learned_pattern.strip() or None) if learned_pattern is not None else None, recommendation.strip()


def _reflection_text(reflection: dict[str, Any]) -> str | None:
    for key in ("text", "answer", "reflection"):
        value = reflection.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _compact(value: str, limit: int = 420) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if len(normalized) <= limit:
        return normalized
    excerpt = normalized[:limit]
    sentence_ends = list(re.finditer(r"[.!?](?=\s|$)", excerpt))
    if sentence_ends and sentence_ends[-1].end() >= limit * 0.4:
        excerpt = excerpt[:sentence_ends[-1].end()]
    else:
        excerpt = excerpt.rsplit(" ", 1)[0]
    return f"{excerpt.rstrip()}…"


def _fallback_analysis(
    *,
    incident: Incident,
    evidence: str,
    memories: list,
    reflection: str | None,
) -> tuple[str | None, str]:
    service = incident.service.replace("-", " ").strip()
    service_lower = service.lower()
    incident_terms = {
        token.rstrip("s")
        for token in re.findall(r"[a-z0-9]+", f"{incident.service} {incident.title}".lower())
        if token not in {"api", "service", "incident", "spike", "failure", "error", "errors", "current", "the", "for"}
    }
    relevant_memories = [
        memory.text.strip()
        for memory in memories
        if memory.text.strip()
        and any(term in f"{memory.text} {memory.context or ''}".lower() for term in incident_terms)
    ]
    recalled_summary = "; ".join(_compact(text, 250) for text in relevant_memories[:2])
    reflection_summary = _compact(reflection) if reflection else None
    learned_pattern = reflection_summary or recalled_summary or None
    downstream_checks = (
        "payment-provider health and payment authorization/error rates"
        if "payment" in service_lower or "payments" in service_lower
        else "downstream dependency health and service error rates"
    )

    steps: list[str] = []
    if recalled_summary:
        steps.append(f"Related Hindsight history: {recalled_summary}.")
    if reflection_summary:
        steps.append(f"Hindsight Reflect: {reflection_summary}")
    if evidence.strip():
        steps.append(f"Current evidence: {_compact(evidence, 250)}")
    steps.append(
        f"For {service}, compare the incident start time with the latest deployment and configuration changes; "
        f"check p95/p99 latency, saturation, {downstream_checks}."
    )
    steps.append(
        "If the symptoms align with a recent change, verify the change is implicated and rollback is safe before reverting it; "
        "then monitor latency and errors to confirm recovery."
    )
    return learned_pattern, " ".join(steps)


async def investigate_incident(
    *,
    incident: Incident,
    evidence: str,
    hindsight: HindsightAdapter,
    settings: Settings,
) -> IncidentInvestigationResponse:
    recalled = await hindsight.recall(
        query=(
            f"Incident investigation for {incident.service}: {incident.title}. "
            f"Current evidence: {evidence or 'No additional evidence provided.'}"
        ),
        types=["experience", "observation", "world"],
        budget="mid",
        max_tokens=2048,
    )
    memories = _memory_items(recalled)
    reflection: str | None = None
    try:
        reflected = await hindsight.reflect(
            query=(
                f"Identify recurring causes and effective mitigations in historical incidents relevant to "
                f"{incident.service}: {incident.title}. Current evidence: {evidence or 'none provided.'}"
            ),
            budget="low",
            max_tokens=1024,
            include_facts=True,
        )
        reflection = _reflection_text(reflected)
    except HindsightError:
        # Recall is the primary evidence source; a missing reflection should not block an investigation.
        reflection = None

    incident_context = {
        "id": incident.id,
        "title": incident.title,
        "service": incident.service,
        "status": incident.status,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "evidence": evidence.strip() or None,
    }
    prompt = (
        "Act as IncidentPilot, an incident-response agent. Analyze the current incident using its title, service, "
        "status, and supplied evidence together with the recalled Hindsight memories. Recommend concrete, prioritized "
        "investigation or mitigation steps. Do not assert an unsupported root cause; distinguish checks from confirmed facts. "
        "Return only a valid JSON object with exactly these fields: "
        '{"learned_pattern": string or null, "recommendation": string}. '
        "Set learned_pattern to a concise pattern supported by the recalled memories, or null if the memories do not establish one. "
        "Make recommendation specific to this incident and useful to an on-call engineer.\n\n"
        f"Current incident: {json.dumps(incident_context, ensure_ascii=False)}"
    )
    if (settings.openai_api_key or "").strip():
        generated = await generate_chat_response(
            settings=settings,
            message=prompt,
            history=[],
            memories=[memory.model_dump() for memory in memories],
            reflection=reflection,
        )
        learned_pattern, recommendation = _analysis_json(generated)
    else:
        learned_pattern, recommendation = _fallback_analysis(
            incident=incident,
            evidence=evidence,
            memories=memories,
            reflection=reflection,
        )
    return IncidentInvestigationResponse(
        incident=IncidentRead.model_validate(incident),
        recalled_memories=memories,
        learned_pattern=learned_pattern,
        recommendation=recommendation,
    )
