"""Typed client for the native Sarvam Samvaad APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from .settings import Settings

logger = logging.getLogger(__name__)

OUTBOUND_BASE_URL = "https://apps.sarvam.ai/api/outbounds/v1"
AUTHORING_BASE_URL = "https://apps.sarvam.ai/api/app-authoring"
ANALYTICS_BASE_URL = "https://apps.sarvam.ai/api/analytics/v1"


class SarvamAPIError(RuntimeError):
    """An API error with a secret-safe message and retry metadata."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
        response_body: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.response_body = response_body or {}


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=1)
    agent_phone_number: str

    @field_validator("agent_phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return _validate_e164(value)


class AppOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_bot_message: str | None = None
    initial_state_name: str | None = None
    initial_language_name: (
        Literal[
            "Bengali",
            "Gujarati",
            "Kannada",
            "Malayalam",
            "Tamil",
            "Telugu",
            "Punjabi",
            "Sanskrit",
            "Odia",
            "Marathi",
            "Hindi",
            "English",
            "Assamese",
        ]
        | None
    ) = None


class OutboundAppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str = Field(min_length=1)
    app_version: int = Field(ge=1)
    connection_config: ConnectionConfig
    agent_variables: dict[str, Any] = Field(default_factory=dict)
    app_type: Literal["agent"] = "agent"
    app_overrides: AppOverrides = Field(default_factory=AppOverrides)


class OutboundUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_phone_number: str

    @field_validator("user_phone_number")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return _validate_e164(value)


class OutboundWebhookConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateOutboundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_config: OutboundAppConfig
    user_config: OutboundUserConfig
    webhook_config: OutboundWebhookConfig


class CreateOutboundResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    attempt_id: str = Field(min_length=1)


class DeploymentConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: str = Field(min_length=1)
    phone_numbers: list[str] = Field(min_length=1)

    @field_validator("phone_numbers")
    @classmethod
    def validate_phones(cls, values: list[str]) -> list[str]:
        return [_validate_e164(value) for value in values]


class InboundConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    allowed_days: list[
        Literal[
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
    ] = Field(min_length=1)
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=100)


class CreateDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=50, pattern=r"^[\w\- ]{1,50}$")
    app_id: str = Field(min_length=1)
    app_version: int = Field(ge=1)
    connection_configs: list[DeploymentConnectionConfig] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=150)
    inbound_config: InboundConfig | None = None


class DeploymentDetails(BaseModel):
    model_config = ConfigDict(extra="allow")

    deployment_id: str
    name: str | None = None
    app_id: str
    app_version: int
    connection_configs: list[DeploymentConnectionConfig]
    channel_direction: Literal["inbound", "outbound", "inbound_outbound"]
    status: Literal["active", "paused"] | None = None
    description: str | None = None
    inbound_config: dict[str, Any] | None = None
    created_by: str
    created_at: datetime
    updated_by: str | None = None
    updated_at: datetime | None = None


class DeploymentSummary(BaseModel):
    """Shape returned by Sarvam's deployment collection endpoint.

    The live list response deliberately flattens phone numbers and omits
    ``connection_configs``. Callers that need to reconcile a binding must fetch the
    deployment detail rather than treating this summary as authoritative.
    """

    model_config = ConfigDict(extra="allow")

    deployment_id: str
    name: str | None = None
    app_id: str
    app_version: int
    phone_numbers: list[str]
    channel_direction: Literal["inbound", "outbound", "inbound_outbound"]
    status: Literal["active", "paused"] | None = None
    description: str | None = None
    inbound_config: dict[str, Any] | None = None
    created_by: str
    created_at: datetime
    updated_by: str | None = None
    updated_at: datetime | None = None

    @field_validator("phone_numbers")
    @classmethod
    def validate_phones(cls, values: list[str]) -> list[str]:
        return [_validate_e164(value) for value in values]


class DeploymentList(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[DeploymentSummary] = Field(default_factory=list)
    total: int = 0
    limit: int = 10
    offset: int = 0
    next_page_uri: str | None = None
    prev_page_uri: str | None = None


class TranscriptTurn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: Literal["agent", "user"]
    en_text: str


class InstantOutboundWebhook(BaseModel):
    """Published post-call reconciliation payload."""

    model_config = ConfigDict(extra="allow")

    attempt_id: str
    status: Literal["connected", "no_answer", "busy", "failed"]
    channel_info: dict[str, Any]
    duration: float | None = None
    interaction_id: str | None = None
    failure_reason: str | None = None
    final_agent_variables: dict[str, Any] | None = None
    webhook_config: dict[str, Any] | None = None
    interaction_transcript: list[TranscriptTurn] | None = None


class SarvamClient:
    """Minimal native API client with bounded retries.

    The API does not document an idempotency-key header for Instant Outbound, so
    duplicate suppression belongs in the Hotline store before this client is called.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={
                "X-API-Key": settings.sarvam_api_key.get_secret_value(),
                "Accept": "application/json",
            },
        )
        self._sleep = sleep
        self._rng = rng or random.Random()

    async def __aenter__(self) -> SarvamClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def build_outbound_request(
        self,
        *,
        event_id: str,
        owner_phone_number: str,
        direction: Literal["outbound", "inbound"] = "outbound",
        trigger: str,
        urgency: str,
        summary: str,
        thread_id: str | None = None,
        initial_bot_message: str | None = None,
        initial_state_name: str | None = None,
        initial_language_name: Literal["English", "Hindi"] | None = "English",
        metadata: Mapping[str, Any] | None = None,
    ) -> CreateOutboundRequest:
        settings = self.settings
        missing = [
            name
            for name, value in (
                ("SARVAM_APP_ID", settings.sarvam_app_id),
                ("SARVAM_CONNECTION_ID", settings.sarvam_connection_id),
                ("SARVAM_AGENT_PHONE_NUMBER", settings.sarvam_agent_phone_number),
                ("PUBLIC_BASE_URL", settings.public_base_url),
                (
                    "HOTLINE_CALLBACK_TOKEN",
                    settings.hotline_callback_token.get_secret_value(),
                ),
            )
            if not value
        ]
        if missing:
            raise SarvamAPIError(
                f"Cannot create outbound request; missing configuration: {', '.join(missing)}"
            )

        variables: dict[str, Any] = {
            "event_id": event_id,
            "direction": direction,
            "trigger": trigger,
            "urgency": urgency,
            "event_summary": summary,
        }
        if thread_id:
            variables["thread_id"] = thread_id

        callback_token = settings.hotline_callback_token.get_secret_value()
        webhook_url = (
            f"{settings.public_base_url}/v1/sarvam/webhooks/instant-outbound/{callback_token}"
        )
        return CreateOutboundRequest(
            app_config=OutboundAppConfig(
                app_id=settings.sarvam_app_id or "",
                app_version=settings.sarvam_app_version,
                connection_config=ConnectionConfig(
                    connection_id=settings.sarvam_connection_id or "",
                    agent_phone_number=settings.sarvam_agent_phone_number or "",
                ),
                agent_variables=variables,
                app_overrides=AppOverrides(
                    initial_bot_message=initial_bot_message,
                    initial_state_name=initial_state_name,
                    initial_language_name=initial_language_name,
                ),
            ),
            user_config=OutboundUserConfig(user_phone_number=owner_phone_number),
            webhook_config=OutboundWebhookConfig(
                url=webhook_url,
                metadata={"event_id": event_id, **dict(metadata or {})},
            ),
        )

    async def create_outbound_call(self, request: CreateOutboundRequest) -> CreateOutboundResponse:
        settings = self.settings
        if not settings.sarvam_org_id or not settings.sarvam_workspace_id:
            raise SarvamAPIError("Sarvam organization/workspace is not configured")
        url = (
            f"{OUTBOUND_BASE_URL}/orgs/{settings.sarvam_org_id}"
            f"/workspaces/{settings.sarvam_workspace_id}/outbounds"
        )
        payload = request.model_dump(mode="json", exclude_none=True)
        result = await self._request_json("POST", url, json_body=payload)
        return CreateOutboundResponse.model_validate(result)

    async def create_inbound_deployment(
        self, request: CreateDeploymentRequest
    ) -> DeploymentDetails:
        url = self._deployment_collection_url()
        result = await self._request_json(
            "POST", url, json_body=request.model_dump(mode="json", exclude_none=True)
        )
        return DeploymentDetails.model_validate(result)

    async def list_deployments(
        self,
        *,
        offset: int = 0,
        limit: int = 100,
        search: str | None = None,
    ) -> DeploymentList:
        params: dict[str, Any] = {"offset": offset, "limit": limit}
        if search:
            params["search"] = search
        result = await self._request_json("GET", self._deployment_collection_url(), params=params)
        return DeploymentList.model_validate(result)

    async def get_deployment(self, deployment_id: str) -> DeploymentDetails:
        if not deployment_id.strip():
            raise SarvamAPIError("Deployment ID must not be empty")
        encoded_id = quote(deployment_id, safe="")
        url = f"{self._deployment_collection_url()}/{encoded_id}"
        result = await self._request_json("GET", url)
        return DeploymentDetails.model_validate(result)

    async def set_deployment_status(
        self, deployment_id: str, action: Literal["pause", "resume"]
    ) -> DeploymentDetails:
        url = f"{self._deployment_collection_url()}/{deployment_id}/status"
        result = await self._request_json("PUT", url, json_body={"action": action})
        return DeploymentDetails.model_validate(result)

    async def get_attempts(
        self,
        *,
        start_datetime: datetime,
        end_datetime: datetime,
        limit: int = 20,
        offset: int = 0,
        filter_conditions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        settings = self.settings
        if not all((settings.sarvam_org_id, settings.sarvam_workspace_id, settings.sarvam_app_id)):
            raise SarvamAPIError("Sarvam analytics identifiers are not configured")
        url = (
            f"{ANALYTICS_BASE_URL}/{settings.sarvam_org_id}/"
            f"{settings.sarvam_workspace_id}/{settings.sarvam_app_id}/attempts"
        )
        params: dict[str, Any] = {
            "start_datetime": start_datetime.isoformat(),
            "end_datetime": end_datetime.isoformat(),
            "limit": limit,
            "offset": offset,
        }
        if filter_conditions:
            params["filter_conditions"] = json.dumps(
                filter_conditions, separators=(",", ":"), sort_keys=True
            )
        return await self._request_json("GET", url, params=params)

    def _deployment_collection_url(self) -> str:
        settings = self.settings
        if not settings.sarvam_org_id or not settings.sarvam_workspace_id:
            raise SarvamAPIError("Sarvam organization/workspace is not configured")
        return (
            f"{AUTHORING_BASE_URL}/v1/orgs/{settings.sarvam_org_id}"
            f"/workspaces/{settings.sarvam_workspace_id}/deployments"
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = self.settings.hotline_retry_attempts + 1
        last_error: SarvamAPIError | None = None

        for index in range(attempts):
            response: httpx.Response | None = None
            try:
                response = await self._client.request(
                    method,
                    url,
                    json=json_body,
                    params=params,
                    headers={
                        "X-API-Key": self.settings.sarvam_api_key.get_secret_value(),
                        "Accept": "application/json",
                    },
                )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as exc:
                last_error = SarvamAPIError(
                    f"Sarvam request failed: {type(exc).__name__}",
                    retriable=True,
                )
            else:
                body = _safe_json(response)
                if 200 <= response.status_code < 300:
                    if not isinstance(body, dict):
                        raise SarvamAPIError(
                            "Sarvam returned a non-object JSON response",
                            status_code=response.status_code,
                        )
                    return body

                retriable = response.status_code == 429 or response.status_code >= 500
                last_error = SarvamAPIError(
                    _error_message(response.status_code, body),
                    status_code=response.status_code,
                    retriable=retriable,
                    response_body=body if isinstance(body, dict) else {},
                )

            if not last_error.retriable or index >= attempts - 1:
                raise last_error

            retry_after = _retry_after_seconds(response) if response is not None else None
            delay = retry_after or min(0.5 * (2**index), 4.0)
            delay += self._rng.uniform(0.0, min(delay * 0.2, 0.5))
            logger.warning(
                "Retrying Sarvam request after transient failure",
                extra={
                    "status_code": last_error.status_code,
                    "attempt": index + 1,
                    "delay_seconds": round(delay, 3),
                },
            )
            await self._sleep(delay)

        raise last_error or SarvamAPIError("Sarvam request failed")


def _validate_e164(value: str) -> str:
    value = value.strip()
    if not value.startswith("+") or not value[1:].isdigit() or not 8 <= len(value[1:]) <= 15:
        raise ValueError("phone number must be E.164, for example +12025550123")
    if value[1] == "0":
        raise ValueError("E.164 country code cannot start with zero")
    return value


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError):
        text = response.text.strip()
        return {"message": text[:500] if text else "empty response"}


def _error_message(status_code: int, body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("code") or "provider error"
        if isinstance(detail, list):
            detail = "; ".join(
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail[:5]
            )
        if detail:
            return f"Sarvam returned HTTP {status_code}: {str(detail)[:500]}"
    return f"Sarvam returned HTTP {status_code}"


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return min(max(value, 0.0), 30.0)
