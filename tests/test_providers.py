from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agent_hotline.contracts import ContactHumanRequest
from agent_hotline.providers import (
    CallPlacementOutcomeUnknownError,
    DisabledCallProvider,
    FakeCallProvider,
    OpenAIRealtimeCallProvider,
    create_call_provider,
)
from agent_hotline.settings import Settings
from agent_hotline.twilio import TwilioCallResult
from agent_hotline.vobiz import VobizAPIError, VobizClient


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


@dataclass(slots=True)
class FailingVobizClient:
    error: VobizAPIError

    async def place_call(
        self,
        event_id: str,
        request: ContactHumanRequest,
    ) -> TwilioCallResult:
        del event_id, request
        raise self.error

    async def end_call(self, call_uuid: str) -> None:
        del call_uuid

    async def close(self) -> None:
        return None


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


@pytest.mark.asyncio
async def test_provider_factory_selects_vobiz_client_for_vobiz_carrier() -> None:
    provider = create_call_provider(
        Settings(
            _env_file=None,
            hotline_transport="openai_realtime",
            hotline_carrier="vobiz",
            vobiz_auth_id="MA_provider01",
            vobiz_auth_token="vobiz-provider-factory-token",
            vobiz_phone_number="+12025550100",
            owner_phone_number="+12025550199",
            public_base_url="https://hotline.example.test",
            hotline_sip_correlation_secret="vobiz-provider-correlation-secret",
        )
    )

    try:
        assert isinstance(provider, OpenAIRealtimeCallProvider)
        assert isinstance(provider.client, VobizClient)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_vobiz_unknown_outcome_is_translated_without_exposing_details() -> None:
    carrier_error = VobizAPIError(
        "upstream response contained secret=vobiz-sensitive-value",
        status_code=503,
        outcome_unknown=True,
    )
    provider = OpenAIRealtimeCallProvider(
        Settings(
            _env_file=None,
            hotline_transport="openai_realtime",
            hotline_carrier="vobiz",
        ),
        client=FailingVobizClient(carrier_error),  # type: ignore[arg-type]
    )

    with pytest.raises(CallPlacementOutcomeUnknownError) as exc_info:
        await provider.place_call("evt_vobiz_unknown", contact_request())

    assert str(exc_info.value) == "carrier call creation has an unknown outcome"
    assert "vobiz-sensitive-value" not in str(exc_info.value)
    assert exc_info.value.__cause__ is carrier_error


@pytest.mark.asyncio
async def test_vobiz_definitive_failure_remains_a_carrier_error() -> None:
    carrier_error = VobizAPIError(
        "Vobiz returned HTTP 400",
        status_code=400,
        outcome_unknown=False,
    )
    provider = OpenAIRealtimeCallProvider(
        Settings(
            _env_file=None,
            hotline_transport="openai_realtime",
            hotline_carrier="vobiz",
        ),
        client=FailingVobizClient(carrier_error),  # type: ignore[arg-type]
    )

    with pytest.raises(VobizAPIError) as exc_info:
        await provider.place_call("evt_vobiz_rejected", contact_request())

    assert exc_info.value is carrier_error
