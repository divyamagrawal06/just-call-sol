"""Client used by the CLI and the thin stdio MCP process."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel

from .contracts import (
    ContactHumanRequest,
    ContactHumanResult,
    EventSummary,
    NotifyHumanRequest,
    RepositoryContextQuery,
    RepositoryContextResponse,
)
from .settings import Settings, get_settings


class HotlineClientError(RuntimeError):
    pass


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    transport: str
    openai_realtime_configured: bool = False
    openai_realtime_runtime_ready: bool = False
    twilio_configured: bool = False
    active_realtime_calls: int = 0
    secure_fallback_configured: bool = False
    codex_app_server: dict[str, Any] | None = None


class HotlineClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._owns_client = client is None
        token = self.settings.hotline_local_token.get_secret_value()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = client or httpx.AsyncClient(
            base_url=self.settings.hotline_daemon_url,
            headers=headers,
            timeout=httpx.Timeout(
                float(self.settings.hotline_decision_timeout_seconds + 30),
                connect=5.0,
            ),
        )

    async def __aenter__(self) -> HotlineClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> HealthResponse:
        return HealthResponse.model_validate(await self._request("GET", "/health"))

    async def contact_human(self, request: ContactHumanRequest) -> ContactHumanResult:
        result = await self._request(
            "POST",
            "/v1/escalations/contact",
            json=request.model_dump(mode="json", exclude_none=True),
            request_timeout=float(request.timeout_seconds + 30),
        )
        return ContactHumanResult.model_validate(result)

    async def start_contact_human(self, request: ContactHumanRequest) -> ContactHumanResult:
        result = await self._request(
            "POST",
            "/v1/escalations/start",
            json=request.model_dump(mode="json", exclude_none=True),
            request_timeout=30.0,
        )
        return ContactHumanResult.model_validate(result)

    async def notify_human(self, request: NotifyHumanRequest) -> ContactHumanResult:
        result = await self._request(
            "POST",
            "/v1/escalations/notify",
            json=request.model_dump(mode="json", exclude_none=True),
            request_timeout=30.0,
        )
        return ContactHumanResult.model_validate(result)

    async def get_event(self, event_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/events/{event_id}")

    async def get_result(self, event_id: str) -> ContactHumanResult:
        payload = await self._request("GET", f"/v1/events/{event_id}/result")
        return ContactHumanResult.model_validate(payload)

    async def list_events(self, limit: int = 20) -> list[EventSummary]:
        payload = await self._request("GET", "/v1/events", params={"limit": limit})
        return [EventSummary.model_validate(item) for item in payload]

    async def repository_context(
        self,
        request: RepositoryContextQuery,
    ) -> RepositoryContextResponse:
        payload = await self._request(
            "POST",
            "/v1/repository/context",
            json=request.model_dump(mode="json", exclude_none=True),
            request_timeout=10.0,
        )
        return RepositoryContextResponse.model_validate(payload)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        request_timeout: float | None = None,
    ) -> Any:
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                params=params,
                timeout=request_timeout,
            )
        except httpx.HTTPError as exc:
            raise HotlineClientError(
                f"Hotline daemon is unavailable: {type(exc).__name__}"
            ) from exc

        if 200 <= response.status_code < 300:
            try:
                return response.json()
            except ValueError as exc:
                raise HotlineClientError("Hotline daemon returned invalid JSON") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("error")
        raise HotlineClientError(
            f"Hotline daemon returned HTTP {response.status_code}"
            + (f": {str(detail)[:500]}" if detail else "")
        )
