from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from agent_hotline.sarvam import (
    CreateDeploymentRequest,
    DeploymentConnectionConfig,
    DeploymentList,
    InstantOutboundWebhook,
    SarvamAPIError,
    SarvamClient,
)
from agent_hotline.settings import Settings


def configured_settings(**overrides):
    values = {
        "sarvam_api_key": SecretStr("test-key"),
        "sarvam_org_id": "org_1",
        "sarvam_workspace_id": "ws_1",
        "sarvam_app_id": "app_1",
        "sarvam_app_version": 2,
        "sarvam_connection_id": "conn_1",
        "sarvam_agent_phone_number": "+918000000001",
        "owner_phone_number": SecretStr("+918000000002"),
        "public_base_url": "https://hotline.example",
        "hotline_callback_token": SecretStr("callback-token"),
        "hotline_retry_attempts": 1,
    }
    values.update(overrides)
    return Settings(**values)


def test_build_outbound_request_uses_exact_native_shape() -> None:
    client = SarvamClient(configured_settings(), client=httpx.AsyncClient())
    request = client.build_outbound_request(
        event_id="evt_123",
        owner_phone_number="+918000000002",
        trigger="incident",
        urgency="critical",
        summary="Database request units are exhausted.",
        thread_id="thr_123",
        initial_bot_message="I need your decision.",
    )
    payload = request.model_dump(mode="json", exclude_none=True)

    assert payload["app_config"]["app_type"] == "agent"
    assert payload["app_config"]["app_version"] == 2
    assert payload["app_config"]["connection_config"] == {
        "connection_id": "conn_1",
        "agent_phone_number": "+918000000001",
    }
    assert payload["app_config"]["agent_variables"]["event_id"] == "evt_123"
    assert payload["user_config"]["user_phone_number"] == "+918000000002"
    assert payload["webhook_config"]["url"].endswith(
        "/v1/sarvam/webhooks/instant-outbound/callback-token"
    )


@pytest.mark.asyncio
async def test_create_outbound_call_parses_attempt_id() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers["X-API-Key"]
        return httpx.Response(200, json={"attempt_id": "attempt_123"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SarvamClient(configured_settings(), client=http_client)
    request = client.build_outbound_request(
        event_id="evt_123",
        owner_phone_number="+918000000002",
        trigger="approval",
        urgency="high",
        summary="Tests passed and deploy needs approval.",
    )

    response = await client.create_outbound_call(request)
    await http_client.aclose()

    assert response.attempt_id == "attempt_123"
    assert seen["url"].endswith("/orgs/org_1/workspaces/ws_1/outbounds")
    assert seen["api_key"] == "test-key"


@pytest.mark.asyncio
async def test_transient_failure_retries_once() -> None:
    calls = 0
    sleeps = []

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"detail": "temporarily unavailable"})
        return httpx.Response(200, json={"attempt_id": "attempt_456"})

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SarvamClient(configured_settings(), client=http_client, sleep=fake_sleep)
    request = client.build_outbound_request(
        event_id="evt_456",
        owner_phone_number="+918000000002",
        trigger="incident",
        urgency="high",
        summary="Service is unavailable.",
    )

    result = await client.create_outbound_call(request)
    await http_client.aclose()

    assert result.attempt_id == "attempt_456"
    assert calls == 2
    assert len(sleeps) == 1


@pytest.mark.asyncio
async def test_non_retriable_error_preserves_safe_detail() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": [{"msg": "invalid app_version"}]})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SarvamClient(configured_settings(), client=http_client)
    request = client.build_outbound_request(
        event_id="evt_bad",
        owner_phone_number="+918000000002",
        trigger="approval",
        urgency="medium",
        summary="Need approval.",
    )

    with pytest.raises(SarvamAPIError, match="invalid app_version") as error:
        await client.create_outbound_call(request)
    await http_client.aclose()
    assert error.value.status_code == 422
    assert not error.value.retriable


def test_webhook_status_is_strict() -> None:
    payload = InstantOutboundWebhook.model_validate(
        {
            "attempt_id": "attempt_1",
            "status": "no_answer",
            "channel_info": {
                "channel_type": "v2v",
                "channel_provider": "vobiz",
                "agent_phone_number": "+918000000001",
            },
            "interaction_id": None,
            "failure_reason": None,
            "interaction_transcript": None,
        }
    )
    assert payload.status == "no_answer"

    with pytest.raises(ValidationError):
        InstantOutboundWebhook.model_validate(
            {
                "attempt_id": "attempt_1",
                "status": "approved",
                "channel_info": {},
            }
        )


def test_deployment_schema_validates_name_and_e164() -> None:
    request = CreateDeploymentRequest(
        name="Agent Hotline",
        app_id="app_1",
        app_version=1,
        connection_configs=[
            DeploymentConnectionConfig(
                connection_id="conn_1",
                phone_numbers=["+918000000001"],
            )
        ],
        description="Inbound owner control",
    )
    assert request.inbound_config is None

    with pytest.raises(ValidationError):
        CreateDeploymentRequest(
            name="Agent Hotline!",
            app_id="app_1",
            app_version=1,
            connection_configs=[
                DeploymentConnectionConfig(
                    connection_id="conn_1",
                    phone_numbers=["08000000001"],
                )
            ],
        )


def test_live_deployment_list_shape_allows_flat_phone_numbers_without_connections() -> None:
    payload = {
        "items": [
            {
                "app_id": "app_1",
                "app_version": 2,
                "channel_direction": "inbound",
                "created_at": "2026-07-26T00:00:00Z",
                "created_by": "owner",
                "deployment_id": "deployment_1",
                "description": "Inbound Hotline",
                "inbound_config": None,
                "name": "Agent Hotline",
                "phone_numbers": ["+918000000001"],
                "status": "active",
                "updated_at": "2026-07-26T00:01:00Z",
                "updated_by": "owner",
            }
        ],
        "limit": 100,
        "next_page_uri": None,
        "offset": 0,
        "prev_page_uri": None,
        "total": 1,
    }

    result = DeploymentList.model_validate(payload)

    assert result.items[0].deployment_id == "deployment_1"
    assert result.items[0].phone_numbers == ["+918000000001"]
    assert not hasattr(result.items[0], "connection_configs")


@pytest.mark.asyncio
async def test_get_deployment_fetches_authoritative_connection_details() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "deployment_id": "deployment_1",
                "name": "Agent Hotline",
                "app_id": "app_1",
                "app_version": 2,
                "connection_configs": [
                    {
                        "connection_id": "conn_1",
                        "phone_numbers": ["+918000000001"],
                    }
                ],
                "channel_direction": "inbound",
                "status": "active",
                "created_by": "owner",
                "created_at": "2026-07-26T00:00:00Z",
            },
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SarvamClient(configured_settings(), client=http_client)

    result = await client.get_deployment("deployment_1")
    await http_client.aclose()

    assert seen["url"].endswith("/workspaces/ws_1/deployments/deployment_1")
    assert result.connection_configs[0].connection_id == "conn_1"


@pytest.mark.asyncio
async def test_analytics_query_encodes_filters() -> None:
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = request.url.params
        return httpx.Response(200, json={"items": []})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = SarvamClient(configured_settings(), client=http_client)
    end = datetime.now(UTC)
    await client.get_attempts(
        start_datetime=end - timedelta(hours=1),
        end_datetime=end,
        filter_conditions=[
            {
                "id": "1",
                "field": "interaction_id",
                "operator": "equals",
                "value": "int_1",
            }
        ],
    )
    await http_client.aclose()
    assert '"field":"interaction_id"' in seen["query"]["filter_conditions"]
