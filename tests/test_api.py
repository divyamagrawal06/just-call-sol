from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError

import agent_hotline.api as api_module
import agent_hotline.storage as storage_module
from agent_hotline.api import create_app
from agent_hotline.codex_protocol import ThreadCandidate, ThreadControlResult
from agent_hotline.contracts import (
    BeginInboundSessionRequest,
    ConfirmActionRequest,
    ContactHumanRequest,
    EscalationContextRequest,
    ExecuteActionRequest,
    PrepareActionRequest,
    RecordInstructionRequest,
    ThreadInspectRequest,
    ThreadListRequest,
)
from agent_hotline.coordinator import HotlineCoordinator
from agent_hotline.models import (
    ContactDirection,
    ContactSession,
    EscalationEvent,
    EventState,
    ProviderWebhookPayload,
    SessionState,
    utc_now,
)
from agent_hotline.openai_realtime import RealtimeToolDispatcher
from agent_hotline.providers import CallAttempt, FakeCallProvider
from agent_hotline.runbooks import create_default_registry
from agent_hotline.settings import Settings
from agent_hotline.storage import (
    ActionHashMismatchError,
    DecisionAlreadyExistsError,
    SQLiteStore,
)

LOCAL_TOKEN = "local-test-token-1234567890-abcdef"
CALLBACK_TOKEN = "action-signing-test-token-1234567890-abcdef"
OWNER_PIN = "246810"


class FakeThreadController:
    def __init__(self) -> None:
        self.extra_candidates: tuple[ThreadCandidate, ...] = ()
        self.list_limits: list[int] = []
        self.searches: list[tuple[str, int]] = []
        self.spawn_calls: list[tuple[str, str]] = []

    def _candidates(self) -> tuple[ThreadCandidate, ...]:
        return (
            ThreadCandidate(
                thread_id="thread-running",
                name="Active task",
                preview="Continue the active repository task.",
                cwd="C:/workspace/just-call-sol",
                status="active",
                updated_at=1,
            ),
            *self.extra_candidates,
        )

    async def list_candidates(
        self,
        *,
        limit: int = 100,
        archived: bool = False,
    ) -> tuple[ThreadCandidate, ...]:
        del archived
        self.list_limits.append(limit)
        return self._candidates()[:limit]

    async def search_candidates(
        self,
        query: str,
        *,
        limit: int = 100,
    ) -> tuple[ThreadCandidate, ...]:
        self.searches.append((query, limit))
        normalized = query.strip().casefold()
        return tuple(
            item
            for item in self._candidates()
            if normalized
            in " ".join(
                (
                    item.thread_id,
                    item.name or "",
                    item.preview,
                    Path(item.cwd).name,
                    item.status,
                )
            ).casefold()
        )[:limit]

    async def inspect_thread(self, reference: str) -> dict[str, Any]:
        return {
            "thread": {
                "id": reference,
                "name": "Active task",
                "preview": "Continue the active repository task.",
                "cwd": "C:/workspace/just-call-sol",
                "status": "active",
                "turns": [],
            }
        }

    async def spawn_root(self, *, task: str, cwd: str) -> ThreadControlResult:
        self.spawn_calls.append((task, cwd))
        return ThreadControlResult(
            action="spawned",
            thread_id="thread-demo-spawned",
            turn_id="turn-demo-spawned",
            response={},
        )


@dataclass(slots=True)
class APIHarness:
    client: httpx.AsyncClient
    provider: FakeCallProvider
    store: SQLiteStore
    coordinator: HotlineCoordinator
    controller: FakeThreadController


@pytest_asyncio.fixture
async def api(tmp_path: Path) -> AsyncIterator[APIHarness]:
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_action_signing_secret=CALLBACK_TOKEN,
        owner_phone_number="+919876543210",
        owner_confirmation_pin=OWNER_PIN,
        hotline_allowlisted_callers="+12025550147",
        codex_app_server_enabled=False,
    )
    provider = FakeCallProvider()
    controller = FakeThreadController()
    app = create_app(
        settings=settings,
        provider=provider,
        controller=controller,  # type: ignore[arg-type]
    )
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
                coordinator=app.state.coordinator,
                controller=controller,
            )


def _contact_payload(
    *,
    wait_for_decision: bool = True,
    dedupe_key: str = "api-test-database-ru-incident",
) -> dict[str, object]:
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
        "dedupe_key": dedupe_key,
        "wait_for_decision": wait_for_decision,
        "timeout_seconds": 5 if wait_for_decision else 1,
    }


def _local_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_TOKEN}"}


async def _wait_for_provider_call(provider: FakeCallProvider) -> tuple[str, object]:
    for _ in range(200):
        if provider.calls:
            return provider.calls[-1]
        await asyncio.sleep(0.01)
    pytest.fail("fake call provider was not invoked")


async def _create_nonblocking_event(api: APIHarness, *, suffix: str) -> str:
    response = await api.client.post(
        "/v1/escalations/notify",
        json=_contact_payload(
            wait_for_decision=False,
            dedupe_key=f"api-test-{suffix}",
        ),
        headers=_local_headers(),
    )
    assert response.status_code == 200
    return response.json()["event_id"]


async def test_runtime_startup_prewarms_unfiltered_top_ten_with_sixty_second_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float | None]] = []

    class StubCodexClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def register_server_request_handler(
            self,
            _method: str,
            _handler: object,
        ) -> None:
            pass

        async def start(self) -> dict[str, object]:
            calls.append(("start", None))
            return {}

        async def close(self) -> None:
            calls.append(("close", None))

    class StubThreadController:
        def __init__(
            self,
            _client: StubCodexClient,
            *,
            workspace_roots: list[Path],
        ) -> None:
            assert workspace_roots == [tmp_path.resolve()]

        async def prewarm_voice_candidates(self, *, timeout_seconds: float) -> bool:
            calls.append(("prewarm-started", timeout_seconds))
            await asyncio.sleep(0)
            calls.append(("prewarm", timeout_seconds))
            return True

    async def idle_monitor(*_args: object) -> None:
        calls.append(("monitor", None))
        await asyncio.Event().wait()

    monkeypatch.setattr(api_module, "CodexAppServerClient", StubCodexClient)
    monkeypatch.setattr(api_module, "SafeThreadController", StubThreadController)
    monkeypatch.setattr(api_module, "_monitor_codex", idle_monitor)
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "runtime-prewarm.sqlite3",
        hotline_transport="fake",
        codex_app_server_enabled=True,
        codex_app_server_cwd=tmp_path,
    )
    app = create_app(settings=settings)

    async with app.router.lifespan_context(app):
        assert calls == [
            ("start", None),
            ("prewarm-started", 60.0),
            ("monitor", None),
            ("prewarm", 60.0),
        ]

    assert calls[-1] == ("close", None)


@pytest.mark.asyncio
async def test_local_api_authentication_and_error_responses_fail_closed(
    api: APIHarness,
) -> None:
    missing = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(),
    )
    invalid = await api.client.post(
        "/v1/escalations/contact",
        json=_contact_payload(),
        headers={"Authorization": "Bearer wrong-token"},
    )
    unauthenticated_start = await api.client.post(
        "/v1/escalations/start",
        json=_contact_payload(),
    )
    crossed = await api.client.post(
        "/v1/repository/context",
        json={"operation": "status"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    blocking_notify = await api.client.post(
        "/v1/escalations/notify",
        json=_contact_payload(wait_for_decision=True),
        headers=_local_headers(),
    )

    assert missing.status_code == 401
    assert invalid.status_code == 403
    assert crossed.status_code == 403
    assert blocking_notify.status_code == 422
    for response in (
        missing,
        invalid,
        unauthenticated_start,
        crossed,
        blocking_notify,
    ):
        assert response.headers["cache-control"] == "no-store"
        assert LOCAL_TOKEN not in response.text
        assert CALLBACK_TOKEN not in response.text


@pytest.mark.asyncio
async def test_start_route_returns_calling_immediately_and_can_be_polled(
    api: APIHarness,
) -> None:
    response = await asyncio.wait_for(
        api.client.post(
            "/v1/escalations/start",
            json=_contact_payload(dedupe_key="api-test-nonblocking-start"),
            headers=_local_headers(),
        ),
        timeout=1,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "calling"
    assert response.json()["outcome"] == "none"
    event_id = response.json()["event_id"]
    assert response.json()["attempt_id"] == f"fake-attempt-{event_id}"
    assert api.provider.calls[-1][1].wait_for_decision is True

    pending = await api.client.get(
        f"/v1/events/{event_id}/result",
        headers=_local_headers(),
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "calling"
    assert pending.json()["identity_verified"] is False

    recorded = await api.coordinator.record_instruction(
        RecordInstructionRequest(
            event_id=event_id,
            outcome="instruct",
            instruction="Keep the task paused while I inspect it.",
            confirmation_pin=OWNER_PIN,
        )
    )
    resolved = await api.client.get(
        f"/v1/events/{event_id}/result",
        headers=_local_headers(),
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["decision_id"] == recorded.decision_id


@pytest.mark.asyncio
async def test_cancelled_contact_start_waits_for_placement_then_terminates_known_call(
    tmp_path: Path,
) -> None:
    class BlockingPlacementProvider(FakeCallProvider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def place_call(
            self,
            event_id: str,
            request: ContactHumanRequest,
        ) -> CallAttempt:
            self.calls.append((event_id, request))
            self.started.set()
            await self.release.wait()
            return CallAttempt(
                attempt_id=f"fake-attempt-{event_id}",
                provider="fake",
            )

    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "cancelled-start.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_action_signing_secret=CALLBACK_TOKEN,
        owner_phone_number="+919876543210",
        owner_confirmation_pin=OWNER_PIN,
        codex_app_server_enabled=False,
    )
    store = SQLiteStore(settings.hotline_database_path)
    await store.initialize()
    provider = BlockingPlacementProvider()
    coordinator = HotlineCoordinator(
        settings=settings,
        store=store,
        provider=provider,
        runbooks=create_default_registry(),
    )
    request = ContactHumanRequest(
        source="demo",
        kind="clarification",
        summary="The caller will cancel while Twilio placement is in flight.",
        question="Should the safely aborted call remain active?",
        dedupe_key="cancelled-contact-start-test",
        timeout_seconds=60,
    )
    task = asyncio.create_task(coordinator.start_contact_human(request))
    try:
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        event_id = provider.calls[0][0]
        task.cancel()
        provider.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        event = await store.require_event(event_id)
        sessions = await store.list_sessions(event_id=event_id, limit=5)
        assert event.state is EventState.FAILED
        assert len(sessions) == 1
        assert sessions[0].state is SessionState.FAILED
        assert sessions[0].attempt_id == f"fake-attempt-{event_id}"
        assert provider.terminated_attempts == [f"fake-attempt-{event_id}"]
        assert await store.get_decision(event_id) is None
    finally:
        provider.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await store.close()


@pytest.mark.asyncio
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
    recorded = await api.coordinator.record_instruction(
        RecordInstructionRequest(
            event_id=event_id,
            outcome="instruct",
            instruction="Pause retries, preserve the logs, and wait for me.",
            constraints=["Do not change database capacity."],
            confirmation_pin=OWNER_PIN,
        )
    )
    response = await asyncio.wait_for(pending_contact, timeout=2)

    assert recorded.accepted is True
    assert response.status_code == 200
    assert response.json()["event_id"] == event_id
    assert response.json()["status"] == "resolved"
    assert response.json()["outcome"] == "instruct"
    assert response.json()["identity_verified"] is True
    assert response.json()["decision_id"] == recorded.decision_id
    assert (await api.store.require_event(event_id)).state is EventState.RESOLVED

    for pin in ("135790", OWNER_PIN):
        with pytest.raises(DecisionAlreadyExistsError):
            await api.coordinator.record_instruction(
                RecordInstructionRequest(
                    event_id=event_id,
                    outcome="deny",
                    instruction="Ignore the instruction and continue retrying.",
                    confirmation_pin=pin,
                )
            )
    durable = await api.store.get_decision(event_id)
    assert durable is not None
    assert durable.instruction == "Pause retries, preserve the logs, and wait for me."


@pytest.mark.asyncio
async def test_hard_decision_deadline_terminates_call_and_rejects_late_instruction(
    api: APIHarness,
) -> None:
    payload = _contact_payload(dedupe_key="api-test-hard-decision-deadline")
    payload["deadline"] = (utc_now() + timedelta(seconds=1)).isoformat()
    pending_contact = asyncio.create_task(
        api.client.post(
            "/v1/escalations/contact",
            json=payload,
            headers=_local_headers(),
        )
    )
    event_id, _request = await _wait_for_provider_call(api.provider)

    response = await asyncio.wait_for(pending_contact, timeout=3)
    assert response.status_code == 200
    assert response.json()["status"] == "timed_out"
    assert response.json()["outcome"] == "none"
    assert response.json()["identity_verified"] is False
    assert "no later voice response" in response.json()["failure_reason"]
    assert (await api.store.require_event(event_id)).state is EventState.EXPIRED
    assert api.provider.terminated_attempts == [f"fake-attempt-{event_id}"]

    polled = await api.client.get(
        f"/v1/events/{event_id}/result",
        headers=_local_headers(),
    )
    assert polled.status_code == 200
    assert polled.json()["status"] == "timed_out"
    assert polled.json()["decision_id"] is None

    with pytest.raises(PermissionError, match="deadline"):
        await api.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=event_id,
                outcome="approve",
                instruction="This arrived after the hard deadline and must be ignored.",
                confirmation_pin=OWNER_PIN,
            )
        )
    assert await api.store.get_decision(event_id) is None


@pytest.mark.asyncio
async def test_background_deadline_expiry_is_durable_and_idempotently_terminates_call(
    api: APIHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = await api.client.post(
        "/v1/escalations/start",
        json=_contact_payload(dedupe_key="api-test-background-deadline-expiry"),
        headers=_local_headers(),
    )
    assert response.status_code == 200
    event_id = response.json()["event_id"]
    event = await api.store.require_event(event_id)
    assert event.deadline_at is not None
    after_deadline = event.deadline_at + timedelta(seconds=1)
    monkeypatch.setattr(storage_module, "utc_now", lambda: after_deadline)

    first = await api.coordinator.expire_decision_deadlines()
    second = await api.coordinator.expire_decision_deadlines()

    assert first == 1
    assert second == 0
    assert (await api.store.require_event(event_id)).state is EventState.EXPIRED
    sessions = await api.store.list_sessions(event_id=event_id, limit=5)
    assert len(sessions) == 1
    assert sessions[0].state is SessionState.CANCELLED
    assert api.provider.terminated_attempts == [f"fake-attempt-{event_id}"]
    polled = await api.client.get(
        f"/v1/events/{event_id}/result",
        headers=_local_headers(),
    )
    assert polled.status_code == 200
    assert polled.json()["status"] == "timed_out"
    assert polled.json()["decision_id"] is None


@pytest.mark.asyncio
async def test_approval_identity_is_derived_only_from_server_verified_pin(
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
    with pytest.raises(ValidationError):
        RecordInstructionRequest(
            event_id=event_id,
            outcome="approve",
            instruction="Approve this one request only.",
            identity_verified=True,
            confirmation_pin=OWNER_PIN,
        )
    with pytest.raises(PermissionError, match="second-factor"):
        await api.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=event_id,
                outcome="approve",
                instruction="Approve this one request only.",
                confirmation_pin="135790",
            )
        )
    assert not pending_contact.done()

    approved = await api.coordinator.record_instruction(
        RecordInstructionRequest(
            event_id=event_id,
            outcome="approve",
            instruction="Approve this one request only.",
            confirmation_pin=OWNER_PIN,
        )
    )
    result = await asyncio.wait_for(pending_contact, timeout=2)

    assert approved.accepted is True
    assert result.json()["outcome"] == "approve"
    assert result.json()["identity_verified"] is True
    assert OWNER_PIN not in approved.model_dump_json() + result.text
    for durable_path in (
        api.store.path,
        Path(f"{api.store.path}-wal"),
        Path(f"{api.store.path}-shm"),
    ):
        if durable_path.exists():
            assert OWNER_PIN.encode() not in durable_path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["approve", "deny", "instruct", "defer", "auth_completed"],
)
async def test_every_decision_outcome_requires_the_configured_pin(
    api: APIHarness,
    outcome: str,
) -> None:
    event_id = await _create_nonblocking_event(api, suffix=f"outcome-{outcome}")
    request = {
        "event_id": event_id,
        "outcome": outcome,
        "instruction": "Record only this confirmed disposition.",
    }

    with pytest.raises(PermissionError, match="second-factor"):
        await api.coordinator.record_instruction(
            RecordInstructionRequest(**request, confirmation_pin="135790")
        )
    accepted = await api.coordinator.record_instruction(
        RecordInstructionRequest(**request, confirmation_pin=OWNER_PIN)
    )

    assert accepted.accepted is True
    decision = await api.store.get_decision(event_id)
    assert decision is not None
    assert decision.outcome.value == outcome
    assert decision.identity_verified is True


@pytest.mark.asyncio
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

    for pin in ("135790", OWNER_PIN):
        with pytest.raises(PermissionError):
            await api.coordinator.record_instruction(
                RecordInstructionRequest(
                    event_id=event.event_id,
                    outcome="instruct",
                    instruction="Continue.",
                    confirmation_pin=pin,
                )
            )
    assert await api.store.get_decision(event.event_id) is None


@pytest.mark.asyncio
async def test_identical_decision_retry_remains_idempotent_after_call_completion(
    api: APIHarness,
) -> None:
    event_id = await _create_nonblocking_event(api, suffix="decision-retry")
    request = RecordInstructionRequest(
        event_id=event_id,
        outcome="instruct",
        instruction="Pause and preserve the logs.",
        confirmation_pin=OWNER_PIN,
    )
    first = await api.coordinator.record_instruction(request)
    await api.coordinator.reconcile_provider_completion(
        ProviderWebhookPayload(
            attempt_id=f"fake-attempt-{event_id}",
            interaction_id="interaction-completed-decision",
            status="connected",
            provider="fake",
            duration_seconds=15,
        )
    )
    replay = await api.coordinator.record_instruction(request)
    wrong_pin_replay = await api.coordinator.record_instruction(
        request.model_copy(update={"confirmation_pin": "135790"})
    )

    assert replay.decision_id == first.decision_id
    assert wrong_pin_replay.decision_id == first.decision_id


@pytest.mark.asyncio
async def test_no_answer_completion_wakes_waiter_without_creating_approval(
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
    prepared = await api.coordinator.prepare_action(
        PrepareActionRequest(
            event_id=event_id,
            action_type="demo.pause_deployment",
            parameters={},
        )
    )
    completed = await api.coordinator.reconcile_provider_completion(
        ProviderWebhookPayload(
            attempt_id=f"fake-attempt-{event_id}",
            status="no_answer",
            provider="fake",
            failure_reason="The owner did not answer.",
        )
    )
    response = await asyncio.wait_for(pending_contact, timeout=2)

    assert completed["created"] is True
    assert response.json()["status"] == "no_answer"
    assert response.json()["outcome"] == "none"
    assert response.json()["identity_verified"] is False
    assert await api.store.get_decision(event_id) is None
    assert (await api.store.require_event(event_id)).state is EventState.FAILED

    with pytest.raises(PermissionError):
        await api.coordinator.confirm_action(
            ConfirmActionRequest(
                event_id=event_id,
                action_id=prepared.action_id,
                confirmation_nonce=prepared.confirmation_nonce,
                exact_confirmation=prepared.exact_readback.rsplit("say exactly: ", 1)[1],
                confirmation_method="spoken_plus_dtmf",
                confirmation_pin=OWNER_PIN,
            )
        )
    with pytest.raises(PermissionError):
        await api.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=event_id,
                outcome="approve",
                instruction="Approve it.",
                confirmation_pin=OWNER_PIN,
            )
        )


@pytest.mark.asyncio
async def test_provider_completion_retries_are_idempotent(api: APIHarness) -> None:
    event_id = await _create_nonblocking_event(api, suffix="completion-retry")
    payload = ProviderWebhookPayload(
        attempt_id=f"fake-attempt-{event_id}",
        status="busy",
        provider="fake",
        failure_reason="Line busy.",
    )

    first = await api.coordinator.reconcile_provider_completion(payload)
    replay = await api.coordinator.reconcile_provider_completion(payload)

    assert first["created"] is True
    assert replay["created"] is False
    assert replay["event_id"] == event_id
    timeline = await api.store.list_timeline(event_id=event_id, limit=100)
    assert sum(entry.kind.value == "webhook_received" for entry in timeline) == 1


@pytest.mark.asyncio
async def test_prepare_confirm_execute_runbook_is_exact_and_one_time(
    api: APIHarness,
) -> None:
    event_id = await _create_nonblocking_event(api, suffix="runbook")
    prepared = await api.coordinator.prepare_action(
        PrepareActionRequest(
            event_id=event_id,
            action_type="demo.increase_db_ru_limit",
            parameters={"target_ru": 800},
            workspace_ref="workspace-demo",
            thread_id="thread-demo",
        )
    )
    exact_phrase = prepared.exact_readback.rsplit("say exactly: ", 1)[1]
    rescoped = await api.coordinator.prepare_action(
        PrepareActionRequest(
            event_id=event_id,
            action_type="demo.increase_db_ru_limit",
            parameters={"target_ru": 800},
            workspace_ref="different-workspace",
            thread_id="thread-demo",
        )
    )
    assert rescoped.action_hash != prepared.action_hash
    assert rescoped.action_id != prepared.action_id

    unverified = await api.coordinator.confirm_action(
        ConfirmActionRequest(
            event_id=event_id,
            action_id=prepared.action_id,
            confirmation_nonce=prepared.confirmation_nonce,
            exact_confirmation=exact_phrase,
            confirmation_method="spoken_plus_dtmf",
            confirmation_pin="135790",
        )
    )
    assert unverified.confirmed is False
    with pytest.raises(ActionHashMismatchError):
        await api.coordinator.confirm_action(
            ConfirmActionRequest(
                event_id=event_id,
                action_id=prepared.action_id,
                confirmation_nonce=prepared.confirmation_nonce,
                exact_confirmation="CONFIRM SOME OTHER ACTION",
                confirmation_method="spoken_plus_dtmf",
                confirmation_pin=OWNER_PIN,
            )
        )
    confirmed = await api.coordinator.confirm_action(
        ConfirmActionRequest(
            event_id=event_id,
            action_id=prepared.action_id,
            confirmation_nonce=prepared.confirmation_nonce,
            exact_confirmation=exact_phrase,
            confirmation_method="spoken_plus_dtmf",
            confirmation_pin=OWNER_PIN,
        )
    )
    assert confirmed.confirmed is True
    assert confirmed.grant_id is not None
    execution_request = ExecuteActionRequest(
        event_id=event_id,
        action_id=prepared.action_id,
        grant_id=confirmed.grant_id,
    )
    executed = await api.coordinator.execute_action(execution_request)
    assert executed.executed is True
    assert executed.result["status"] == "mock_succeeded"
    assert executed.result["verified"] is True
    replayed = await api.coordinator.execute_action(execution_request)
    assert replayed == executed


@pytest.mark.asyncio
async def test_outbound_voice_action_and_final_decision_wake_waiter_with_result(
    api: APIHarness,
) -> None:
    pending_contact = asyncio.create_task(
        api.client.post(
            "/v1/escalations/contact",
            json=_contact_payload(
                dedupe_key="api-test-complete-outbound-action-flow",
            ),
            headers=_local_headers(),
        )
    )
    event_id, _request = await _wait_for_provider_call(api.provider)
    session = (await api.store.list_sessions(event_id=event_id, limit=1))[0]
    dispatcher = RealtimeToolDispatcher(
        settings=api.coordinator.settings,
        coordinator=api.coordinator,
        event_id=event_id,
        session_id=session.session_id,
        direction="outbound_escalation",
    )

    async def verify_prepared_readback(
        prepared: dict[str, Any],
        *,
        response_id: str,
    ) -> None:
        readback = prepared.get("response_text") or prepared.get("exact_readback")
        assert isinstance(readback, str)
        dispatcher.note_readback_transcript(response_id, readback)
        dispatcher.note_response_done(response_id)
        dispatcher.note_output_audio_stopped(response_id)
        dispatcher.note_owner_speech_started()
        dispatcher.note_owner_speech_stopped()
        armed = await dispatcher.dispatch("arm_owner_verification", {})
        assert armed.payload["armed"] is True
        statuses = [
            status
            for digit in f"{OWNER_PIN}#"
            if (status := dispatcher.receive_dtmf(digit)) is not None
        ]
        assert statuses == [
            {
                "verified": True,
                "scope": prepared["scope"],
                "subject_id": prepared["subject_id"],
                "message": (
                    "Trusted server signal: keypad verification succeeded for the current "
                    "prepared request. Continue only with that exact request."
                ),
            }
        ]

    prepared_action = await dispatcher.dispatch(
        "prepare_action",
        {
            "action_type": "demo.increase_db_ru_limit",
            "parameters": {"target_ru": 800},
            "workspace_ref": "workspace-demo",
            "thread_id": "thread-demo",
        },
    )
    action_id = prepared_action.payload["action_id"]
    action_scope = {
        **prepared_action.payload,
        "scope": "action",
        "subject_id": action_id,
    }
    await verify_prepared_readback(
        action_scope,
        response_id="resp_complete_action_readback",
    )
    exact_phrase = prepared_action.payload["exact_readback"].rsplit(
        "say exactly: ",
        1,
    )[1]
    confirmed = await dispatcher.dispatch(
        "confirm_action",
        {
            "action_id": action_id,
            "exact_confirmation": exact_phrase,
        },
    )
    assert confirmed.payload["confirmed"] is True

    executed = await dispatcher.dispatch(
        "execute_action",
        {"action_id": action_id},
    )
    assert executed.payload["executed"] is True
    assert executed.payload["result"]["status"] == "mock_succeeded"

    prepared_decision = await dispatcher.dispatch(
        "prepare_decision",
        {
            "outcome": "instruct",
            "instruction": "Keep the database at 800 RUs and resume requests.",
            "constraints": ["Do not exceed 800 RUs."],
            "approved_action_ids": [action_id],
        },
    )
    decision_scope = {
        **prepared_decision.payload,
        "scope": "decision",
        "subject_id": prepared_decision.payload["confirmation_id"],
    }
    await verify_prepared_readback(
        decision_scope,
        response_id="resp_complete_decision_readback",
    )
    recorded = await dispatcher.dispatch(
        "record_decision",
        {"confirmation_id": prepared_decision.payload["confirmation_id"]},
    )
    assert recorded.payload["accepted"] is True

    response = await asyncio.wait_for(pending_contact, timeout=2)
    payload = response.json()
    assert response.status_code == 200
    assert payload["status"] == "resolved"
    assert payload["outcome"] == "instruct"
    assert payload["approved_action_ids"] == [action_id]
    assert payload["action_results"] == [
        {
            "action_id": action_id,
            "status": "succeeded",
            "message_to_user": executed.payload["message_to_user"],
            "result": executed.payload["result"],
            "retryable": False,
            "operation_id": executed.payload["operation_id"],
        }
    ]


@pytest.mark.asyncio
async def test_inbound_control_discloses_nothing_to_unallowlisted_callers(
    api: APIHarness,
) -> None:
    rejected = await api.coordinator.begin_inbound_session(
        BeginInboundSessionRequest(
            caller_phone_number="+12025550148",
            interaction_id="interaction-unlisted",
        )
    )
    malformed = await api.coordinator.begin_inbound_session(
        BeginInboundSessionRequest(
            caller_phone_number="not-a-phone",
            interaction_id="interaction-malformed",
        )
    )
    accepted = await api.coordinator.begin_inbound_session(
        BeginInboundSessionRequest(
            caller_phone_number="0012025550147",
            interaction_id="interaction-owner",
        )
    )
    replay = await api.coordinator.begin_inbound_session(
        BeginInboundSessionRequest(
            caller_phone_number="+12025550147",
            interaction_id="interaction-owner",
        )
    )

    assert rejected.accepted is False
    assert rejected.event_id is None
    assert rejected.identity_verified is False
    assert malformed.accepted is False
    assert accepted.accepted is True
    assert accepted.identity_verified is False
    assert accepted.event_id is not None
    assert replay.event_id == accepted.event_id
    listed = await api.coordinator.list_threads(ThreadListRequest(event_id=accepted.event_id))
    inspected = await api.coordinator.inspect_thread(
        ThreadInspectRequest(
            event_id=accepted.event_id,
            reference="thread-running",
        )
    )
    assert listed["threads"]
    assert inspected["thread_id"] == "thread-running"
    events = await api.store.list_events(limit=10)
    assert [event.event_id for event in events] == [accepted.event_id]


@pytest.mark.asyncio
async def test_thread_reads_accept_only_provider_correlated_live_calls(
    api: APIHarness,
) -> None:
    event_id = await _create_nonblocking_event(api, suffix="thread-reads")
    listed = await api.coordinator.list_threads(ThreadListRequest(event_id=event_id))
    inspected = await api.coordinator.inspect_thread(
        ThreadInspectRequest(event_id=event_id, reference="thread-running")
    )
    context = await api.coordinator.escalation_context(EscalationContextRequest(event_id=event_id))

    assert [item["thread_id"] for item in listed["threads"]] == ["thread-running"]
    assert inspected["thread_id"] == "thread-running"
    assert context.event_id == event_id

    await api.coordinator.reconcile_provider_completion(
        ProviderWebhookPayload(
            attempt_id=f"fake-attempt-{event_id}",
            status="connected",
            provider="fake",
            duration_seconds=5,
        )
    )
    with pytest.raises(PermissionError):
        await api.coordinator.list_threads(ThreadListRequest(event_id=event_id))
    with pytest.raises(PermissionError):
        await api.coordinator.inspect_thread(
            ThreadInspectRequest(event_id=event_id, reference="thread-running")
        )


@pytest.mark.asyncio
async def test_thread_list_uses_warm_cache_for_exact_running_status_queries(
    api: APIHarness,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_id = await _create_nonblocking_event(api, suffix="warm-cache")
    decoys = tuple(
        ThreadCandidate(
            thread_id=f"thread-decoy-{index}",
            name=f"Decoy {index}",
            preview="An unrelated historical task.",
            cwd="C:/workspace/just-call-sol",
            status="idle",
            updated_at=0,
        )
        for index in range(10)
    )
    api.controller.extra_candidates = (
        ThreadCandidate(
            thread_id="thread-idle-history",
            name="Historical task",
            preview="A running total is documented here.",
            cwd="C:/workspace/just-call-sol",
            status="idle",
            updated_at=0,
        ),
        *decoys,
        ThreadCandidate(
            thread_id="thread-active-beyond-cache",
            name="Late active task",
            preview="This match is beyond the cached top ten.",
            cwd="C:/workspace/just-call-sol",
            status="active",
            updated_at=0,
        ),
    )
    caplog.set_level(logging.INFO, logger="agent_hotline.coordinator")

    active = await api.coordinator.list_threads(
        ThreadListRequest(event_id=event_id, query="active")
    )
    running = await api.coordinator.list_threads(
        ThreadListRequest(event_id=event_id, query="running", limit=25)
    )
    in_progress = await api.coordinator.list_threads(
        ThreadListRequest(event_id=event_id, query="in progress")
    )
    absent = await api.coordinator.list_threads(
        ThreadListRequest(event_id=event_id, query="definitely-absent")
    )

    assert [item["thread_id"] for item in active["threads"]] == [
        "thread-running",
        "thread-active-beyond-cache",
    ]
    assert [item["thread_id"] for item in running["threads"]] == ["thread-running"]
    assert [item["thread_id"] for item in in_progress["threads"]] == ["thread-running"]
    assert absent["threads"] == []
    assert api.controller.list_limits[-2:] == [10, 10]
    assert api.controller.searches[-2:] == [
        ("active", 10),
        ("definitely-absent", 10),
    ]
    duration_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_hotline.coordinator"
        and record.getMessage().startswith("voice_list_threads_live_session_guard")
    ]
    assert len(duration_messages) == 4
    assert all("duration_ms=" in message for message in duration_messages)
    assert all(event_id not in message for message in duration_messages)
    assert all("running" not in message for message in duration_messages)


@pytest.mark.asyncio
async def test_voice_tools_reject_an_uncorrelated_outbound_session(
    api: APIHarness,
) -> None:
    event = EscalationEvent(kind="status", summary="Uncorrelated outbound call.")
    await api.store.create_event(event)
    await api.store.transition_event(event.event_id, EventState.QUEUED)
    await api.store.transition_event(event.event_id, EventState.DIALING)
    await api.store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
        )
    )

    async def operations() -> None:
        await api.coordinator.list_threads(ThreadListRequest(event_id=event.event_id))

    with pytest.raises(PermissionError):
        await operations()
    with pytest.raises(PermissionError):
        await api.coordinator.inspect_thread(
            ThreadInspectRequest(
                event_id=event.event_id,
                reference="thread-running",
            )
        )
    with pytest.raises(PermissionError):
        await api.coordinator.escalation_context(EscalationContextRequest(event_id=event.event_id))
    with pytest.raises(PermissionError):
        await api.coordinator.prepare_action(
            PrepareActionRequest(
                event_id=event.event_id,
                action_type="demo.pause_deployment",
                parameters={},
            )
        )


@pytest.mark.asyncio
async def test_missing_owner_pin_configuration_denies_grants_and_approvals(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "missing-pin.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_action_signing_secret=CALLBACK_TOKEN,
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
                "/v1/escalations/notify",
                json=_contact_payload(wait_for_decision=False),
                headers=_local_headers(),
            )
            event_id = created.json()["event_id"]
            coordinator: HotlineCoordinator = app.state.coordinator
            with pytest.raises(PermissionError, match="second-factor"):
                await coordinator.record_instruction(
                    RecordInstructionRequest(
                        event_id=event_id,
                        outcome="approve",
                        instruction="Approve this operation.",
                        confirmation_pin=OWNER_PIN,
                    )
                )
            prepared = await coordinator.prepare_action(
                PrepareActionRequest(
                    event_id=event_id,
                    action_type="demo.pause_deployment",
                    parameters={},
                )
            )
            rejected_grant = await coordinator.confirm_action(
                ConfirmActionRequest(
                    event_id=event_id,
                    action_id=prepared.action_id,
                    confirmation_nonce=prepared.confirmation_nonce,
                    exact_confirmation=prepared.exact_readback.rsplit(
                        "say exactly: ",
                        1,
                    )[1],
                    confirmation_method="spoken_plus_dtmf",
                    confirmation_pin=OWNER_PIN,
                )
            )

    assert rejected_grant.confirmed is False
    assert OWNER_PIN not in rejected_grant.model_dump_json()
