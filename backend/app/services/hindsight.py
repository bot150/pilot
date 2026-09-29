from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Settings, get_settings

TIMEOUT_SECONDS = 30.0
API_PREFIX = "/v1/default/banks"


class HindsightError(Exception):
    """Safe, user-facing failure raised by the Hindsight integration."""

    def __init__(self, message: str, *, status_code: int, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class HindsightConfigurationError(HindsightError):
    def __init__(self, missing: list[str]) -> None:
        names = ", ".join(missing)
        super().__init__(
            f"Hindsight is not configured. Set: {names}.",
            status_code=503,
            code="hindsight_not_configured",
        )


class HindsightUnavailableError(HindsightError):
    def __init__(self) -> None:
        super().__init__(
            "The Hindsight service could not be reached.",
            status_code=502,
            code="hindsight_unavailable",
        )


class HindsightTimeoutError(HindsightError):
    def __init__(self) -> None:
        super().__init__(
            "The Hindsight service timed out.",
            status_code=504,
            code="hindsight_timeout",
        )


class HindsightUpstreamError(HindsightError):
    def __init__(self, upstream_status: int) -> None:
        hint = " Check the API key and bank access." if upstream_status in (401, 403) else ""
        super().__init__(
            f"Hindsight rejected the request (HTTP {upstream_status}).{hint}",
            status_code=502,
            code="hindsight_request_failed",
        )


class HindsightProtocolError(HindsightError):
    def __init__(self) -> None:
        super().__init__(
            "Hindsight returned an invalid response.",
            status_code=502,
            code="hindsight_invalid_response",
        )


class HindsightAdapter:
    """Small HTTP boundary for Hindsight's retain, recall, and reflect API."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport

    def _configuration(self) -> tuple[str, str, str]:
        values = {
            "HINDSIGHT_API_URL": self._settings.hindsight_api_url,
            "HINDSIGHT_API_KEY": self._settings.hindsight_api_key,
            "HINDSIGHT_BANK_ID": self._settings.hindsight_bank_id,
        }
        missing = [name for name, value in values.items() if not value or not value.strip()]
        if missing:
            raise HindsightConfigurationError(missing)

        base_url = self._settings.hindsight_api_url.strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HindsightError(
                "HINDSIGHT_API_URL must be an absolute HTTP or HTTPS base URL.",
                status_code=503,
                code="hindsight_invalid_configuration",
            )
        return base_url, self._settings.hindsight_api_key.strip(), self._settings.hindsight_bank_id.strip()

    async def _post(self, operation_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        base_url, api_key, bank_id = self._configuration()
        encoded_bank_id = quote(bank_id, safe="")
        url = f"{base_url}{API_PREFIX}/{encoded_bank_id}/{operation_path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT_SECONDS,
                transport=self._transport,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise HindsightTimeoutError() from exc
        except httpx.RequestError as exc:
            raise HindsightUnavailableError() from exc

        if response.is_error:
            raise HindsightUpstreamError(response.status_code)
        try:
            result = response.json()
        except ValueError as exc:
            raise HindsightProtocolError() from exc
        if not isinstance(result, dict):
            raise HindsightProtocolError()
        return result

    async def retain(
        self,
        *,
        content: str,
        context: str | None = None,
        document_id: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {"content": content}
        if context is not None:
            item["context"] = context
        if document_id is not None:
            item["document_id"] = document_id
        if timestamp is not None:
            item["timestamp"] = timestamp
        return await self._post("memories", {"items": [item], "async": False})

    async def retain_many(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Retain a related set of memory chunks in one Hindsight request."""
        if not items:
            raise ValueError("At least one memory item is required.")
        return await self._post("memories", {"items": items, "async": False})

    async def recall(
        self,
        *,
        query: str,
        types: list[str] | None = None,
        budget: str = "mid",
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens}
        if types is not None:
            payload["types"] = types
        return await self._post("memories/recall", payload)

    async def reflect(
        self,
        *,
        query: str,
        budget: str = "low",
        max_tokens: int = 4096,
        include_facts: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens}
        if include_facts:
            payload["include"] = {"facts": {}}
        return await self._post("reflect", payload)


def get_hindsight_adapter() -> HindsightAdapter:
    return HindsightAdapter(get_settings())
