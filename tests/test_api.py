from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from agent_hotline.api import create_app
from agent_hotline.models import (
    ContactDirection,
    ContactSession,
    EscalationEvent,
    EventState,
    SessionState,
)
from agent_hotline.providers import FakeCallProvider
from agent_hotline.settings import Settings
from agent_hotline.storage import SQLiteStore

LOCAL_TOKEN = "local-test-token-1234567890"
TOOL_TOKEN = "tool-test-token-12345678901"
CALLBACK_TOKEN = "callback-test-token-123456"
OWNER_PIN = "246810"


@dataclass(slots=True)
class APIHarness:
    client: httpx.AsyncClient
    provider: FakeCallProvider
    store: SQLiteStore


@pytest_asyncio.fixture
async def api(tmp_path: Path) -> AsyncIterator[APIHarness]:
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_tool_token=TOOL_TOKEN,
        hotline_callback_token=CALLBACK_TOKEN,
        owner_phone_number="+919876543210",
        owner_confirmation_pin=OWNER_PIN,
        hotline_allowlisted_callers="+12025550147",
        codex_app_server_enabled=False,
    )
    provider = FakeCallProvider()
    app = create_app(settings=settings, provider=provider)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield APIHarness(
                client=client,
                provider=provider,
                store=app.state.store,
            )


def _contact_payload(*, wait_for_decision: bool = True) -> dict[str, object]:
    return {
        "source": "codex_mcp",
        "kind": "incident",
        "severity": "critical",
        "summary": "The demo database is out of request units.",
        "question": "Should the agent pause and wait for an instruction?",
        "context": {
            "thread_id": "thread-demo",
            "workspace_ref": "workspace-demo",
            "last_error": "Every request is returning a throttling error.",
        },
        "dedupe_key": "api-test-database-ru-incident",
        "wait_for_decision": wait_for_decision,
        "timeout_seconds": 5,
    }


def _local_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_TOKEN}"}


def _tool_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOOL_TOKEN}"}


async def _wait_for_provider_call(provider: FakeCallProvider) -> tuple[str, object]:
    for _ in range(200):
        if provider.calls:
            return provider.calls[-1]
        await asyncio.sleep(0.01)
    pytest.fail("fake call provider was not invoked")


async def test_api_authentication_boundaries_are_separate_and_fail_closed(
    api: APIHarness,
) -> None:
    missing = await api.client.post("/v1/escalations/contact", json=_contact_payload())
    invalid = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(),
        headers={"Authorization": "Bearer wrong-token"},
    )
    crossed_tokens = await api.client.post(
        "/v1/sarvam/tools/context",
        json={"event_id": "evt_does-not-exist"},
        headers=_local_headers(),
    )
    accepted_auth = await api.client.post(
        "/v1/sarvam/tools/context",
        json={"event_id": "evt_does-not-exist"},
        headers=_tool_headers(),
    )
    blocking_notify = await api.client.post(
        "/v1/escalations/notify",
        json={
            **_contact_payload(wait_for_decision=False),
            "wait_for_decision": True,
            "timeout_seconds": 5,
        },
        headers=_local_headers(),
    )
    invalid_callback = await api.client.post(
        "/v1/sarvam/webhooks/instant-outbound/not-the-callback-token",
        json={
            "attempt_id": "attempt-unknown",
            "status": "no_answer",
            "channel_info": {},
        },
    )

    assert missing.status_code == 401
    assert invalid.status_code == 403
    assert crossed_tokens.status_code == 403
    assert accepted_auth.status_code == 404
    assert blocking_notify.status_code == 422
    assert invalid_callback.status_code == 404
    for response in (
        missing,
        invalid,
        crossed_tokens,
        accepted_auth,
        blocking_notify,
        invalid_callback,
    ):
        assert response.headers["cache-control"] == "no-store"
        assert LOCAL_TOKEN not in response.text
        assert TOOL_TOKEN not in response.text
        assert CALLBACK_TOKEN not in response.text


async def test_contact_waiter_is_woken_by_authoritative_mid_call_decision(
    api: APIHarness,
) -> None:
    pending_contact = asyncio.create_task(
        api.client.post(
            "/v1/escalations/contact",
            json=_contact_payload(),
            headers=_local_headers(),
        )
    )
    event_id, _request = await _wait_for_provider_call(api.provider)

    recorded = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={
            "event_id": event_id,
            "outcome": "instruct",
            "instruction": "Pause retries, preserve the logs, and wait for me.",
            "constraints": ["Do not change database capacity."],
            "confirmation_pin": OWNER_PIN,
        },
        headers=_tool_headers(),
    )
    response = await asyncio.wait_for(pending_contact, timeout=2)

    assert recorded.status_code == 200
    assert recorded.json()["accepted"] is True
    assert response.status_code == 200
    assert response.json() == {
        "event_id": event_id,
        "status": "resolved",
        "outcome": "instruct",
        "instruction": "Pause retries, preserve the logs, and wait for me.",
        "constraints": ["Do not change database capacity."],
        "approved_action_ids": [],
        "identity_verified": True,
        "decision_id": recorded.json()["decision_id"],
        "attempt_id": f"fake-attempt-{event_id}",
        "channel": "voice",
        "failure_reason": None,
        "created_at": response.json()["created_at"],
        "decision_recorded_at": response.json()["decision_recorded_at"],
    }
    assert (await api.store.require_event(event_id)).state is EventState.RESOLVED

    conflicting_wrong_pin = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={
            "event_id": event_id,
            "outcome": "deny",
            "instruction": "Ignore the instruction and continue retrying.",
            "confirmation_pin": "135790",
        },
        headers=_tool_headers(),
    )
    conflicting_replay = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={
            "event_id": event_id,
            "outcome": "deny",
            "instruction": "Ignore the instruction and continue retrying.",
            "confirmation_pin": OWNER_PIN,
        },
        headers=_tool_headers(),
    )
    assert conflicting_wrong_pin.status_code == 409
    assert conflicting_replay.status_code == 409
    durable = await api.store.get_decision(event_id)
    assert durable is not None
    assert durable.instruction == "Pause retries, preserve the logs, and wait for me."


async def test_approval_identity_is_derived_only_from_the_configured_pin(
    api: APIHarness,
) -> None:
    pending_contact = asyncio.create_task(
        api.client.post(
            "/v1/escalations/contact",
            json=_contact_payload(),
            headers=_local_headers(),
        )
    )
    event_id, _request = await _wait_for_provider_call(api.provider)
    request = {
        "event_id": event_id,
        "outcome": "approve",
        "instruction": "Approve this one request only.",
        "confirmation_method": "spoken_plus_dtmf",
    }

    forged_identity = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "identity_verified": True, "confirmation_pin": OWNER_PIN},
        headers=_tool_headers(),
    )
    wrong_pin = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": "135790"},
        headers=_tool_headers(),
    )
    assert forged_identity.status_code == 422
    assert wrong_pin.status_code == 403
    assert not pending_contact.done()

    approved = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": OWNER_PIN},
        headers=_tool_headers(),
    )
    result = await asyncio.wait_for(pending_contact, timeout=2)

    assert approved.status_code == 200
    assert result.status_code == 200
    assert result.json()["status"] == "resolved"
    assert result.json()["outcome"] == "approve"
    assert result.json()["identity_verified"] is True
    assert OWNER_PIN not in approved.text + result.text
    for durable_path in (
        api.store.path,
        Path(f"{api.store.path}-wal"),
        Path(f"{api.store.path}-shm"),
    ):
        if durable_path.exists():
            assert OWNER_PIN.encode("utf-8") not in durable_path.read_bytes()


@pytest.mark.parametrize(
    "outcome",
    ["approve", "deny", "instruct", "defer", "auth_completed"],
)
async def test_every_decision_outcome_requires_the_configured_pin(
    api: APIHarness,
    outcome: str,
) -> None:
    created = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(wait_for_decision=False),
        headers=_local_headers(),
    )
    event_id = created.json()["event_id"]
    request = {
        "event_id": event_id,
        "outcome": outcome,
        "instruction": "Record only this confirmed disposition.",
        "confirmation_method": "spoken_plus_dtmf",
    }

    missing = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json=request,
        headers=_tool_headers(),
    )
    wrong = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": "135790"},
        headers=_tool_headers(),
    )
    accepted = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": OWNER_PIN},
        headers=_tool_headers(),
    )

    assert missing.status_code == 422
    assert wrong.status_code == 403
    assert accepted.status_code == 200
    decision = await api.store.get_decision(event_id)
    assert decision is not None
    assert decision.outcome.value == outcome
    assert decision.identity_verified is True


@pytest.mark.parametrize(
    ("direction", "state", "attempt_id", "interaction_id"),
    [
        (ContactDirection.INBOUND_CONTROL, SessionState.DIALING, None, "interaction-1"),
        (ContactDirection.INBOUND_CONTROL, SessionState.CONNECTED, None, None),
        (
            ContactDirection.OUTBOUND_ESCALATION,
            SessionState.DIALING,
            "attempt-1",
            None,
        ),
    ],
)
async def test_invalid_decision_session_is_rejected_before_pin_comparison(
    api: APIHarness,
    direction: ContactDirection,
    state: SessionState,
    attempt_id: str | None,
    interaction_id: str | None,
) -> None:
    event = EscalationEvent(
        kind="status",
        summary="Inbound control test.",
        evidence={"caller_allowlisted": True, "direction": "inbound"},
    )
    await api.store.create_event(event)
    await api.store.transition_event(event.event_id, EventState.QUEUED)
    await api.store.transition_event(event.event_id, EventState.DIALING)
    if state is SessionState.CONNECTED:
        await api.store.transition_event(event.event_id, EventState.CONNECTED)
    await api.store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=direction,
            state=state,
            attempt_id=attempt_id,
            interaction_id=interaction_id,
        )
    )
    request = {
        "event_id": event.event_id,
        "outcome": "instruct",
        "instruction": "Continue.",
    }

    wrong = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": "135790"},
        headers=_tool_headers(),
    )
    correct = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": OWNER_PIN},
        headers=_tool_headers(),
    )

    assert wrong.status_code == 403
    assert correct.status_code == 403
    assert await api.store.get_decision(event.event_id) is None


async def test_identical_decision_retry_remains_idempotent_after_call_completion(
    api: APIHarness,
) -> None:
    created = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(wait_for_decision=False),
        headers=_local_headers(),
    )
    event_id = created.json()["event_id"]
    request = {
        "event_id": event_id,
        "outcome": "instruct",
        "instruction": "Pause and preserve the logs.",
        "confirmation_pin": OWNER_PIN,
    }
    first = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json=request,
        headers=_tool_headers(),
    )
    completed = await api.client.post(
        f"/v1/sarvam/webhooks/instant-outbound/{CALLBACK_TOKEN}",
        json={
            "attempt_id": f"fake-attempt-{event_id}",
            "interaction_id": "interaction-completed-decision",
            "status": "connected",
            "channel_info": {"direction": "outbound"},
            "duration": 15,
        },
    )
    replay = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json=request,
        headers=_tool_headers(),
    )
    wrong_pin_replay = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={**request, "confirmation_pin": "135790"},
        headers=_tool_headers(),
    )

    assert first.status_code == 200
    assert completed.status_code == 200
    assert replay.status_code == 200
    assert wrong_pin_replay.status_code == 200
    assert replay.json()["decision_id"] == first.json()["decision_id"]
    assert wrong_pin_replay.json()["decision_id"] == first.json()["decision_id"]


async def test_no_answer_webhook_wakes_waiter_without_creating_approval(
    api: APIHarness,
) -> None:
    pending_contact = asyncio.create_task(
        api.client.post(
            "/v1/escalations/contact",
            json=_contact_payload(),
            headers=_local_headers(),
        )
    )
    event_id, _request = await _wait_for_provider_call(api.provider)
    webhook_payload = {
        "attempt_id": f"fake-attempt-{event_id}",
        "status": "no_answer",
        "channel_info": {"direction": "outbound"},
        "failure_reason": "The owner did not answer.",
    }
    prepared = await api.client.post(
        "/v1/sarvam/tools/prepare-action",
        json={
            "event_id": event_id,
            "action_type": "demo.pause_deployment",
            "parameters": {},
        },
        headers=_tool_headers(),
    )
    assert prepared.status_code == 200
    prepared_payload = prepared.json()
    exact_phrase = prepared_payload["exact_readback"].rsplit("say exactly: ", 1)[1]

    webhook = await api.client.post(
        f"/v1/sarvam/webhooks/instant-outbound/{CALLBACK_TOKEN}",
        json=webhook_payload,
    )
    response = await asyncio.wait_for(pending_contact, timeout=2)

    assert webhook.status_code == 200
    assert response.status_code == 200
    assert response.json()["status"] == "no_answer"
    assert response.json()["outcome"] == "none"
    assert response.json()["identity_verified"] is False
    assert await api.store.get_decision(event_id) is None
    assert (await api.store.require_event(event_id)).state is EventState.FAILED

    late_confirmation = await api.client.post(
        "/v1/sarvam/tools/confirm-action",
        json={
            "event_id": event_id,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": exact_phrase,
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": OWNER_PIN,
        },
        headers=_tool_headers(),
    )
    late_approval = await api.client.post(
        "/v1/sarvam/tools/record-instruction",
        json={
            "event_id": event_id,
            "outcome": "approve",
            "instruction": "Approve it.",
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": OWNER_PIN,
        },
        headers=_tool_headers(),
    )
    assert late_confirmation.status_code == 403
    assert late_approval.status_code == 403
    assert await api.store.get_decision(event_id) is None


async def test_completion_webhook_retries_are_idempotent(api: APIHarness) -> None:
    created = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(wait_for_decision=False),
        headers=_local_headers(),
    )
    assert created.status_code == 200
    event_id = created.json()["event_id"]
    payload = {
        "attempt_id": f"fake-attempt-{event_id}",
        "status": "busy",
        "channel_info": {"direction": "outbound"},
        "failure_reason": "Line busy.",
    }

    first = await api.client.post(
        f"/v1/sarvam/webhooks/instant-outbound/{CALLBACK_TOKEN}",
        json=payload,
    )
    replay = await api.client.post(
        f"/v1/sarvam/webhooks/instant-outbound/{CALLBACK_TOKEN}",
        json=payload,
    )

    assert first.status_code == 200
    assert first.json()["created"] is True
    assert replay.status_code == 200
    assert replay.json()["created"] is False
    assert replay.json()["event_id"] == event_id
    timeline = await api.store.list_timeline(event_id=event_id, limit=100)
    assert sum(entry.kind.value == "webhook_received" for entry in timeline) == 1


async def test_prepare_confirm_execute_runbook_is_exact_and_one_time(
    api: APIHarness,
) -> None:
    created = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(wait_for_decision=False),
        headers=_local_headers(),
    )
    event_id = created.json()["event_id"]

    prepared = await api.client.post(
        "/v1/sarvam/tools/prepare-action",
        json={
            "event_id": event_id,
            "action_type": "demo.increase_db_ru_limit",
            "parameters": {"target_ru": 800},
            "workspace_ref": "workspace-demo",
            "thread_id": "thread-demo",
            "commit_or_state_hash": "state-before-increase",
        },
        headers=_tool_headers(),
    )
    assert prepared.status_code == 200
    prepared_payload = prepared.json()
    exact_phrase = prepared_payload["exact_readback"].rsplit("say exactly: ", 1)[1]
    rescoped = await api.client.post(
        "/v1/sarvam/tools/prepare-action",
        json={
            "event_id": event_id,
            "action_type": "demo.increase_db_ru_limit",
            "parameters": {"target_ru": 800},
            "workspace_ref": "different-workspace",
            "thread_id": "thread-demo",
            "commit_or_state_hash": "different-state",
        },
        headers=_tool_headers(),
    )
    assert rescoped.status_code == 200
    assert rescoped.json()["action_hash"] != prepared_payload["action_hash"]
    assert rescoped.json()["action_id"] != prepared_payload["action_id"]

    unverified = await api.client.post(
        "/v1/sarvam/tools/confirm-action",
        json={
            "event_id": event_id,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": exact_phrase,
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": "135790",
        },
        headers=_tool_headers(),
    )
    wrong_readback = await api.client.post(
        "/v1/sarvam/tools/confirm-action",
        json={
            "event_id": event_id,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": "CONFIRM SOME OTHER ACTION",
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": OWNER_PIN,
        },
        headers=_tool_headers(),
    )
    confirmed = await api.client.post(
        "/v1/sarvam/tools/confirm-action",
        json={
            "event_id": event_id,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": exact_phrase,
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": OWNER_PIN,
        },
        headers=_tool_headers(),
    )

    assert unverified.status_code == 200
    assert unverified.json()["confirmed"] is False
    assert wrong_readback.status_code == 409
    assert confirmed.status_code == 200
    assert confirmed.json()["confirmed"] is True

    execution_request = {
        "event_id": event_id,
        "action_id": prepared_payload["action_id"],
        "grant_id": confirmed.json()["grant_id"],
    }
    executed = await api.client.post(
        "/v1/sarvam/tools/execute-action",
        json=execution_request,
        headers=_tool_headers(),
    )
    replay = await api.client.post(
        "/v1/sarvam/tools/execute-action",
        json=execution_request,
        headers=_tool_headers(),
    )

    assert executed.status_code == 200
    assert executed.json()["executed"] is True
    assert executed.json()["result"]["status"] == "mock_succeeded"
    assert executed.json()["result"]["verified"] is True
    assert replay.status_code == 409


async def test_inbound_control_discloses_nothing_to_unallowlisted_callers(
    api: APIHarness,
) -> None:
    rejected = await api.client.post(
        "/v1/sarvam/tools/begin-inbound",
        json={
            "caller_phone_number": "+12025550148",
            "interaction_id": "interaction-unlisted",
        },
        headers=_tool_headers(),
    )
    malformed = await api.client.post(
        "/v1/sarvam/tools/begin-inbound",
        json={
            "caller_phone_number": "not-a-phone",
            "interaction_id": "interaction-malformed",
        },
        headers=_tool_headers(),
    )
    accepted = await api.client.post(
        "/v1/sarvam/tools/begin-inbound",
        json={
            "caller_phone_number": "0012025550147",
            "interaction_id": "interaction-owner",
        },
        headers=_tool_headers(),
    )
    replay = await api.client.post(
        "/v1/sarvam/tools/begin-inbound",
        json={
            "caller_phone_number": "+12025550147",
            "interaction_id": "interaction-owner",
        },
        headers=_tool_headers(),
    )

    assert rejected.status_code == 200
    assert rejected.json()["accepted"] is False
    assert rejected.json()["event_id"] is None
    assert rejected.json()["identity_verified"] is False
    assert malformed.status_code == 200
    assert malformed.json()["accepted"] is False
    assert accepted.status_code == 200
    assert accepted.json()["accepted"] is True
    assert accepted.json()["identity_verified"] is False
    assert accepted.json()["event_id"].startswith("evt_")
    assert replay.status_code == 200
    assert replay.json()["accepted"] is True
    assert replay.json()["identity_verified"] is False
    assert replay.json()["event_id"] == accepted.json()["event_id"]
    events = await api.store.list_events(limit=10)
    assert [event.event_id for event in events] == [accepted.json()["event_id"]]


async def test_missing_owner_pin_configuration_denies_grants_and_approvals(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "missing-pin.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_tool_token=TOOL_TOKEN,
        hotline_callback_token=CALLBACK_TOKEN,
        owner_phone_number="+919876543210",
        owner_confirmation_pin="",
        codex_app_server_enabled=False,
    )
    app = create_app(settings=settings, provider=FakeCallProvider())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/v1/escalations/contact",
                json=_contact_payload(wait_for_decision=False),
                headers=_local_headers(),
            )
            event_id = created.json()["event_id"]
            rejected_approval = await client.post(
                "/v1/sarvam/tools/record-instruction",
                json={
                    "event_id": event_id,
                    "outcome": "approve",
                    "instruction": "Approve this operation.",
                    "confirmation_method": "spoken_plus_dtmf",
                    "confirmation_pin": OWNER_PIN,
                },
                headers=_tool_headers(),
            )
            prepared = await client.post(
                "/v1/sarvam/tools/prepare-action",
                json={
                    "event_id": event_id,
                    "action_type": "demo.pause_deployment",
                    "parameters": {},
                },
                headers=_tool_headers(),
            )
            prepared_payload = prepared.json()
            rejected_grant = await client.post(
                "/v1/sarvam/tools/confirm-action",
                json={
                    "event_id": event_id,
                    "action_id": prepared_payload["action_id"],
                    "confirmation_nonce": prepared_payload["confirmation_nonce"],
                    "exact_confirmation": prepared_payload["exact_readback"].rsplit(
                        "say exactly: ",
                        1,
                    )[1],
                    "confirmation_method": "spoken_plus_dtmf",
                    "confirmation_pin": OWNER_PIN,
                },
                headers=_tool_headers(),
            )

    assert rejected_approval.status_code == 403
    assert rejected_grant.status_code == 200
    assert rejected_grant.json()["confirmed"] is False
    assert OWNER_PIN not in rejected_approval.text + rejected_grant.text
