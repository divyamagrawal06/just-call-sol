from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_hotline.contracts import ContactHumanRequest
from agent_hotline.providers import (
    DisabledCallProvider,
    FakeCallProvider,
    OpenAIRealtimeCallProvider,
    create_call_provider,
)
from agent_hotline.settings import Settings
from agent_hotline.twilio import TwilioCallResult


def contact_request() -> ContactHumanRequest:
    return ContactHumanRequest(
        source="demo",
        kind="incident",
        severity="critical",
        summary="Database requests are failing.",
        question="Should the agent pause or retry?",
        timeout_seconds=1,
    )


@dataclass(slots=True)
class RecordingTwilioClient:
    calls: list[tuple[str, ContactHumanRequest]] = field(default_factory=list)
    ended_calls: list[str] = field(default_factory=list)
    closed: bool = False

    async def place_call(
        self,
        event_id: str,
        request: ContactHumanRequest,
    ) -> TwilioCallResult:
        self.calls.append((event_id, request))
        return TwilioCallResult(attempt_id="CA" + "1" * 32)

    async def end_call(self, call_sid: str) -> None:
        self.ended_calls.append(call_sid)

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_realtime_provider_delegates_only_carrier_origination() -> None:
    settings = Settings(_env_file=None, hotline_transport="openai_realtime")
    client = RecordingTwilioClient()
    provider = OpenAIRealtimeCallProvider(settings, client=client)  # type: ignore[arg-type]
    request = contact_request()

    attempt = await provider.place_call("evt_provider_test", request)
    await provider.terminate_call(attempt.attempt_id)
    await provider.close()

    assert client.calls == [("evt_provider_test", request)]
    assert client.ended_calls == ["CA" + "1" * 32]
    assert attempt.attempt_id == "CA" + "1" * 32
    assert attempt.provider == "openai_realtime"
    assert client.closed is False


@pytest.mark.asyncio
async def test_fake_provider_is_deterministic_and_records_exact_request() -> None:
    provider = FakeCallProvider()
    request = contact_request()

    first = await provider.place_call("evt_fake_test", request)
    second = await provider.place_call("evt_fake_test", request)
    await provider.terminate_call(first.attempt_id)

    assert first == second
    assert first.attempt_id == "fake-attempt-evt_fake_test"
    assert first.provider == "fake"
    assert provider.calls == [
        ("evt_fake_test", request),
        ("evt_fake_test", request),
    ]
    assert provider.terminated_attempts == ["fake-attempt-evt_fake_test"]


@pytest.mark.asyncio
async def test_disabled_provider_fails_closed() -> None:
    provider = DisabledCallProvider()

    with pytest.raises(RuntimeError, match="transport is disabled"):
        await provider.place_call("evt_disabled_test", contact_request())


def test_provider_factory_selects_only_supported_transports() -> None:
    realtime = create_call_provider(
        Settings(
            _env_file=None,
            hotline_transport="openai_realtime",
            twilio_account_sid="AC" + "1" * 32,
            twilio_auth_token="twilio-provider-factory-secret",
            twilio_phone_number="+12025550100",
            owner_phone_number="+12025550199",
            openai_project_id="proj_provider_factory",
            public_base_url="https://hotline.example.test",
            hotline_sip_correlation_secret=("provider-correlation-secret-1234567890"),
        )
    )
    fake = create_call_provider(Settings(_env_file=None, hotline_transport="fake"))
    disabled = create_call_provider(Settings(_env_file=None, hotline_transport="disabled"))

    assert isinstance(realtime, OpenAIRealtimeCallProvider)
    assert isinstance(fake, FakeCallProvider)
    assert isinstance(disabled, DisabledCallProvider)
