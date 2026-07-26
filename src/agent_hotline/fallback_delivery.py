"""Provider-neutral delivery for missed-call secure fallback links."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from .settings import Settings


class FallbackDeliveryError(RuntimeError):
    """Raised without including a bearer link or provider response body."""


@dataclass(frozen=True, slots=True)
class FallbackNotification:
    fallback_id: str
    event_id: str
    secure_url: SecretStr
    expires_at: datetime


class FallbackNotifier(Protocol):
    async def send(self, notification: FallbackNotification) -> None: ...

    async def close(self) -> None: ...


class WebhookFallbackNotifier:
    """Send a generic payload to an owner-controlled SMS or push bridge."""

    def __init__(
        self,
        *,
        url: str,
        token: SecretStr,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = _validate_delivery_url(url)
        if len(token.get_secret_value()) < 16:
            raise ValueError("fallback webhook token must contain at least 16 characters")
        self._token = token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
        )

    async def send(self, notification: FallbackNotification) -> None:
        payload = {
            "type": "agent_hotline.missed_call",
            "version": 1,
            "event_id": notification.event_id,
            "title": "Agent Hotline needs your decision",
            "message": (
                "A call from Agent Hotline was missed. Open the secure one-time link "
                "to review and respond."
            ),
            "url": notification.secure_url.get_secret_value(),
            "expires_at": notification.expires_at.isoformat(),
        }
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Content-Type": "application/json",
            "Idempotency-Key": notification.fallback_id,
        }
        for attempt in range(2):
            try:
                response = await self._client.post(self.url, json=payload, headers=headers)
            except httpx.RequestError as exc:
                if attempt == 0:
                    await asyncio.sleep(0)
                    continue
                raise FallbackDeliveryError("fallback webhook was unreachable") from exc
            if 200 <= response.status_code < 300:
                return
            if response.status_code >= 500 and attempt == 0:
                await asyncio.sleep(0)
                continue
            raise FallbackDeliveryError(
                f"fallback webhook rejected delivery (status={response.status_code})"
            )
        raise FallbackDeliveryError("fallback webhook delivery failed")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def create_fallback_notifier(settings: Settings) -> FallbackNotifier | None:
    if not settings.secure_fallback_configured:
        return None
    assert settings.hotline_fallback_webhook_url is not None
    return WebhookFallbackNotifier(
        url=settings.hotline_fallback_webhook_url,
        token=settings.hotline_fallback_webhook_token,
    )


def _validate_delivery_url(value: str) -> str:
    stripped = value.strip()
    parsed = urlparse(stripped)
    if parsed.scheme == "https" and parsed.hostname:
        return stripped
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}:
        return stripped
    raise ValueError("fallback webhook URL must use HTTPS or loopback HTTP")


__all__ = [
    "FallbackDeliveryError",
    "FallbackNotification",
    "FallbackNotifier",
    "WebhookFallbackNotifier",
    "create_fallback_notifier",
]
