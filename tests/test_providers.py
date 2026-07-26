from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr

from agent_hotline.contracts import ContactHumanRequest
from agent_hotline.providers import SarvamCallProvider
from agent_hotline.sarvam import SarvamClient
from agent_hotline.settings import Settings


def configured_settings() -> Settings:
    return Settings(
        _env_file=None,
        sarvam_api_key=SecretStr("test-key"),
        sarvam_org_id="org_test",
        sarvam_workspace_id="workspace_test",
        sarvam_app_id="app_test",
        sarvam_app_version=1,
        sarvam_connection_id="connection_test",
        sarvam_agent_phone_number="+12025550123",
        owner_phone_number=SecretStr("+12025550199"),
        public_base_url="https://hotline.example.test",
        hotline_callback_token=SecretStr("callback-test-token"),
        hotline_retry_attempts=0,
    )


@pytest.mark.asyncio
async def test_provider_omits_flat_agent_state_override() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"attempt_id": "attempt_test"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = configured_settings()
    sarvam = SarvamClient(settings, client=http_client)
    provider = SarvamCallProvider(settings, client=sarvam)
    request = ContactHumanRequest(
        source="demo",
        kind="incident",
        severity="critical",
        summary="Database requests are failing.",
        question="Should the agent pause or retry?",
        timeout_seconds=1,
    )

    attempt = await provider.place_call("event_test", request)
    await http_client.aclose()

    assert attempt.attempt_id == "attempt_test"
    app_overrides = seen["app_config"]["app_overrides"]  # type: ignore[index]
    assert "initial_state_name" not in app_overrides
    assert app_overrides["initial_language_name"] == "English"


@pytest.mark.asyncio
async def test_builder_includes_state_override_only_when_explicitly_requested() -> None:
    settings = configured_settings()
    http_client = httpx.AsyncClient()
    client = SarvamClient(settings, client=http_client)

    without_state = client.build_outbound_request(
        event_id="event_without_state",
        owner_phone_number="+12025550199",
        trigger="incident",
        urgency="critical",
        summary="Database requests are failing.",
    ).model_dump(mode="json", exclude_none=True)
    with_state = client.build_outbound_request(
        event_id="event_with_state",
        owner_phone_number="+12025550199",
        trigger="incident",
        urgency="critical",
        summary="Database requests are failing.",
        initial_state_name="known_state",
    ).model_dump(mode="json", exclude_none=True)
    await http_client.aclose()

    assert "initial_state_name" not in without_state["app_config"]["app_overrides"]
    assert with_state["app_config"]["app_overrides"]["initial_state_name"] == "known_state"
