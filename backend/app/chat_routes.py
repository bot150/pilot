import re
from uuid import uuid4
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.chat_schemas import ChatMemory, ChatRequest, ChatResponse
from app.config import Settings, get_settings
from app.services.hindsight import HindsightAdapter, HindsightError, get_hindsight_adapter
from app.services.llm import LLMError, generate_chat_response

chat_router = APIRouter(prefix="/api/chat", tags=["AI chat"])
FACT_PATTERN = re.compile(r"^\s*(?:i['’]m|i am|my\b|we['’]re|we are|remember(?: that)?\b)", re.IGNORECASE)
REFLECT_PATTERN = re.compile(r"\b(reflect|pattern|insight|lesson|recommend|suggest|what should|what do you notice)\b", re.IGNORECASE)


def _memory_items(value: Any) -> list[ChatMemory]:
    if isinstance(value, list):
        result: list[ChatMemory] = []
        for index, item in enumerate(value):
            result.extend(_memory_items(item) if isinstance(item, (list, dict)) else _memory_from_scalar(item, index))
        return result
    if not isinstance(value, dict):
        return []

    for key in ("results", "memories", "items", "facts", "episodes"):
        nested = value.get(key)
        if isinstance(nested, (list, dict)):
            memories = _memory_items(nested)
            if memories or isinstance(nested, list):
                return memories

    return _memory_from_dict(value, 0)


def _memory_from_scalar(value: Any, index: int) -> list[ChatMemory]:
    if isinstance(value, str) and value.strip():
        return [ChatMemory(id=str(index), text=value.strip())]
    return []


def _memory_from_dict(value: dict[str, Any], index: int) -> list[ChatMemory]:
    text = next((value.get(key) for key in ("text", "content", "summary", "description", "fact", "memory") if isinstance(value.get(key), str) and value.get(key).strip()), None)
    if not text:
        return []
    kind = next((value.get(key) for key in ("type", "memory_type", "category") if isinstance(value.get(key), str)), "Memory")
    context = next((value.get(key) for key in ("context", "source", "document_id") if isinstance(value.get(key), str)), None)
    date = next((value.get(key) for key in ("timestamp", "created_at", "date") if isinstance(value.get(key), str)), None)
    return [ChatMemory(id=str(value.get("id") or value.get("memory_id") or index), type=kind, text=text.strip(), context=context, date=date)]


def _useful_memory_text(message: str) -> str | None:
    candidates = re.split(r"(?<=[.!?])\s+", message.strip())
    facts = [part.strip() for part in candidates if "?" not in part and FACT_PATTERN.search(part)]
    return " ".join(facts)[:10_000] or None


@chat_router.post("", response_model=ChatResponse)
async def chat(
    payload: ChatRequest,
    hindsight: HindsightAdapter = Depends(get_hindsight_adapter),
    settings: Settings = Depends(get_settings),
) -> ChatResponse:
    if not (settings.openai_api_key or "").strip():
        raise HTTPException(
            status_code=503,
            detail="Set OPENAI_API_KEY in the backend environment to enable AI responses.",
        )

    memories: list[ChatMemory] = []
    reflection: str | None = None
    recall_warning: str | None = None
    try:
        recalled = await hindsight.recall(
            query=payload.message,
            types=["experience", "observation", "world"],
            budget="mid",
            max_tokens=2048,
        )
        memories = _memory_items(recalled)
    except HindsightError:
        recall_warning = "I couldn't reach Hindsight memory for this reply."

    if REFLECT_PATTERN.search(payload.message):
        try:
            reflected = await hindsight.reflect(query=payload.message, budget="low", max_tokens=1024, include_facts=True)
            reflection = next((reflected.get(key) for key in ("text", "answer", "reflection") if isinstance(reflected.get(key), str)), None)
        except HindsightError:
            reflection = None

    try:
        answer = await generate_chat_response(
            settings=settings,
            message=payload.message,
            history=[item.model_dump() for item in payload.history],
            memories=[memory.model_dump() for memory in memories],
            reflection=reflection,
        )
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    retained = False
    retained_text = _useful_memory_text(payload.message)
    retain_warning: str | None = None
    if retained_text:
        try:
            await hindsight.retain(
                content=retained_text,
                context="Chat conversation",
                document_id=f"chat-{uuid4().hex}",
            )
            retained = True
        except HindsightError:
            retain_warning = "I answered, but couldn't save that useful detail to Hindsight."

    return ChatResponse(
        answer=answer,
        memories=memories,
        reflected=bool(reflection),
        retained=retained,
        retained_text=retained_text if retained else None,
        recall_warning=recall_warning,
        retain_warning=retain_warning,
    )
