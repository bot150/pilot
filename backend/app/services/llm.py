from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
LLM_TIMEOUT_SECONDS = 60.0


class LLMError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _response_text(data: dict[str, Any]) -> str:
    output: list[str] = []
    for item in data.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    output.append(text.strip())
    return "\n".join(output)


async def generate_chat_response(
    *,
    settings: Settings,
    message: str,
    history: list[dict[str, str]],
    memories: list[dict[str, str | None]],
    reflection: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise LLMError("Set OPENAI_API_KEY in the backend environment to enable AI responses.", status_code=503)

    memory_context = "\n".join(f"- {memory['text']}" for memory in memories if memory.get("text"))
    instructions = (
        "You are Hindsight AI, a helpful conversational assistant. Answer the user's current request directly and naturally. "
        "Use the provided saved memories only when relevant, and never claim a fact about the user unless supported by the current conversation or those memories. "
        "If memory context is irrelevant, ignore it. Do not describe internal API calls."
    )
    if memory_context:
        instructions += f"\n\nRelevant context recalled from the user's Hindsight memory bank:\n{memory_context}"
    if reflection:
        instructions += f"\n\nAdditional Hindsight reflection to consider as context, not as a substitute for answering the user:\n{reflection}"

    input_messages = [
        {"role": item["role"], "content": item["content"]}
        for item in history[-12:]
        if item.get("role") in {"user", "assistant"} and item.get("content")
    ]
    input_messages.append({"role": "user", "content": message})
    payload = {
        "model": settings.openai_model,
        "instructions": instructions,
        "input": input_messages,
        "store": False,
    }

    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(
                OPENAI_RESPONSES_URL,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            )
    except httpx.TimeoutException as exc:
        raise LLMError("The AI response timed out. Please try again.", status_code=504) from exc
    except httpx.RequestError as exc:
        raise LLMError("The AI service could not be reached.", status_code=502) from exc

    if response.is_error:
        suffix = " Check OPENAI_API_KEY." if response.status_code in {401, 403} else ""
        raise LLMError(f"The AI service rejected the request (HTTP {response.status_code}).{suffix}", status_code=502)
    try:
        data = response.json()
    except ValueError as exc:
        raise LLMError("The AI service returned an invalid response.", status_code=502) from exc
    answer = _response_text(data) if isinstance(data, dict) else ""
    if not answer:
        raise LLMError("The AI service returned no answer.", status_code=502)
    return answer
