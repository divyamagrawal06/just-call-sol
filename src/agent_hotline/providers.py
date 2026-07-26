"""Provider seam kept intentionally narrow until the native slice is proven."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contracts import ContactHumanRequest
from .sarvam import SarvamClient
from .settings import Settings


@dataclass(frozen=True, slots=True)
class CallAttempt:
    attempt_id: str
    provider: str


class CallProvider(Protocol):
    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt: ...

    async def close(self) -> None: ...


class SarvamCallProvider:
    def __init__(self, settings: Settings, client: SarvamClient | None = None) -> None:
        self.settings = settings
        self.client = client or SarvamClient(settings)
        self._owns_client = client is None

    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt:
        owner_phone = self.settings.owner_phone_number.get_secret_value()
        outbound = self.client.build_outbound_request(
            event_id=event_id,
            owner_phone_number=owner_phone,
            trigger=request.kind,
            urgency=request.severity,
            summary=request.summary,
            thread_id=request.context.thread_id,
            initial_bot_message=_initial_message(request),
            initial_language_name="English",
            metadata={
                "source": request.source,
                "dedupe_key": request.dedupe_key,
            },
        )
        result = await self.client.create_outbound_call(outbound)
        return CallAttempt(attempt_id=result.attempt_id, provider="sarvam")

    async def close(self) -> None:
        if self._owns_client:
            await self.client.close()


class FakeCallProvider:
    """Deterministic provider used by tests and the backup demo."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ContactHumanRequest]] = []

    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt:
        self.calls.append((event_id, request))
        return CallAttempt(attempt_id=f"fake-attempt-{event_id}", provider="fake")

    async def close(self) -> None:
        return None


class DisabledCallProvider:
    async def place_call(self, event_id: str, request: ContactHumanRequest) -> CallAttempt:
        del event_id, request
        raise RuntimeError("Call transport is disabled")

    async def close(self) -> None:
        return None


def create_call_provider(settings: Settings) -> CallProvider:
    if settings.hotline_transport == "sarvam":
        return SarvamCallProvider(settings)
    if settings.hotline_transport == "fake":
        return FakeCallProvider()
    return DisabledCallProvider()


def _initial_message(request: ContactHumanRequest) -> str:
    return (
        f"Hi, this is Agent Hotline. {request.summary} I need your decision: {request.question}"
    )[:1200]
