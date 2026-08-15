"""Outbound carrier seam; conversation runtimes are owned separately by the daemon."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import ContactHumanRequest
from .settings import Settings
from .twilio import TwilioAPIError, TwilioClient
from .vobiz import VobizAPIError, VobizClient


@dataclass(frozen=True, slots=True)
class CallAttempt:
    attempt_id: str
    provider: str


class CallPlacementOutcomeUnknownError(RuntimeError):
    """Raised when the carrier may have created a call but returned no usable ID."""


class CallProvider(Protocol):
    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt: ...

    async def terminate_call(self, attempt_id: str) -> None: ...

    async def close(self) -> None: ...


class OpenAIRealtimeCallProvider:
    """Originate PSTN through the selected carrier and bridge it to OpenAI SIP."""

    def __init__(
        self,
        settings: Settings,
        client: TwilioClient | VobizClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or (
            VobizClient(settings)
            if settings.hotline_carrier == "vobiz"
            else TwilioClient(settings)
        )
        self._owns_client = client is None

    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt:
        try:
            result = await self.client.place_call(event_id, request)
        except (TwilioAPIError, VobizAPIError) as exc:
            if exc.outcome_unknown:
                raise CallPlacementOutcomeUnknownError(
                    "carrier call creation has an unknown outcome"
                ) from exc
            raise
        return CallAttempt(
            attempt_id=result.attempt_id,
            provider="openai_realtime",
        )

    async def terminate_call(self, attempt_id: str) -> None:
        await self.client.end_call(attempt_id)

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()


class FakeCallProvider:
    """Deterministic provider used by tests and the backup demo."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ContactHumanRequest]] = []
        self.terminated_attempts: list[str] = []

    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt:
        self.calls.append((event_id, request))
        return CallAttempt(attempt_id=f"fake-attempt-{event_id}", provider="fake")

    async def terminate_call(self, attempt_id: str) -> None:
        self.terminated_attempts.append(attempt_id)

    async def close(self) -> None:
        return None


class DisabledCallProvider:
    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt:
        del event_id, request
        raise RuntimeError("Call transport is disabled")

    async def terminate_call(self, attempt_id: str) -> None:
        del attempt_id

    async def close(self) -> None:
        return None


def create_call_provider(settings: Settings) -> CallProvider:
    if settings.hotline_transport == "openai_realtime":
        return OpenAIRealtimeCallProvider(settings)
    if settings.hotline_transport == "fake":
        return FakeCallProvider()
    return DisabledCallProvider()
