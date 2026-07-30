from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

import agent_hotline.storage as storage_module
from agent_hotline.models import (
    ActionKind,
    ActionScope,
    ActionState,
    ConfirmationMethod,
    ContactDirection,
    ContactSession,
    ContextSnapshot,
    Decision,
    EscalationEvent,
    EventState,
    PreparedAction,
    ProviderWebhookPayload,
    RiskLevel,
    SessionState,
    TimelineEntry,
    TimelineKind,
    TranscriptRole,
    TranscriptTurn,
    new_id,
    utc_now,
)
from agent_hotline.storage import (
    ActionAlreadyConsumedError,
    ActionExpiredError,
    ActionHashMismatchError,
    ActiveSessionError,
    ConflictError,
    CorrelationError,
    DecisionAlreadyExistsError,
    EventDeadlineExpiredError,
    InvalidStateTransitionError,
    SQLiteStore,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def event_factory(
    *,
    dedupe_key: str = "workspace:thread:database-down",
    kind: str = "incident",
) -> EscalationEvent:
    values: dict[str, object] = {
        "kind": kind,
        "summary": "The database has exhausted request units",
        "dedupe_key": dedupe_key,
    }
    if kind == "approval":
        values["question"] = "May I increase capacity?"
    return EscalationEvent(**values)


@pytest_asyncio.fixture
async def store(tmp_path: Path):
    database = SQLiteStore(tmp_path / "hotline.sqlite3")
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


async def test_initializes_wal_foreign_keys_and_schema(store: SQLiteStore) -> None:
    assert str(await store.pragma("journal_mode")).lower() == "wal"
    assert await store.pragma("foreign_keys") == 1
    assert await store.pragma("user_version") == 6


async def test_carrier_admission_replay_is_bound_to_one_provider_call(
    store: SQLiteStore,
) -> None:
    issued_at = utc_now()
    admission = await store.issue_carrier_admission(
        "CA" + ("a" * 32),
        caller_phone="+12025550123",
        ttl_seconds=300,
        issued_at=issued_at,
    )
    arguments = {
        "caller_phone": admission.caller_phone,
        "admission_nonce": admission.admission_nonce,
        "expires_at_epoch": int(admission.expires_at.timestamp()),
        "provider_call_id": "call_openai_one",
    }

    first = await store.consume_carrier_admission(
        admission.call_sid,
        consumed_at=issued_at + timedelta(seconds=1),
        **arguments,
    )
    exact_replay = await store.consume_carrier_admission(
        admission.call_sid,
        consumed_at=issued_at + timedelta(seconds=2),
        **arguments,
    )

    assert first == exact_replay
    assert first.consumed_by_call_id == "call_openai_one"
    with pytest.raises(ConflictError, match="already consumed"):
        await store.consume_carrier_admission(
            admission.call_sid,
            consumed_at=issued_at + timedelta(seconds=3),
            **{**arguments, "provider_call_id": "call_openai_two"},
        )


async def test_revoking_unconsumed_admission_atomically_queues_parent_termination(
    store: SQLiteStore,
) -> None:
    issued_at = utc_now()
    call_sid = "CA" + ("d" * 32)
    admission = await store.issue_carrier_admission(
        call_sid,
        caller_phone="+12025550123",
        ttl_seconds=300,
        issued_at=issued_at,
    )
    revoked_at = issued_at + timedelta(seconds=1)

    jobs = await store.revoke_unconsumed_carrier_admissions(
        revoked_at=revoked_at,
    )
    replay = await store.revoke_unconsumed_carrier_admissions(
        revoked_at=revoked_at + timedelta(seconds=1),
    )

    assert len(jobs) == 1
    assert replay == []
    assert jobs[0].leg.value == "carrier"
    assert jobs[0].target_id == call_sid
    assert jobs[0].state.value == "pending"
    assert jobs[0].attempts == 0
    with pytest.raises(CorrelationError, match="does not match the SIP leg"):
        await store.consume_carrier_admission(
            call_sid,
            caller_phone=admission.caller_phone,
            admission_nonce=admission.admission_nonce,
            expires_at_epoch=int(admission.expires_at.timestamp()),
            provider_call_id="call_delayed_after_revoke",
            consumed_at=revoked_at + timedelta(microseconds=1),
        )
    assert await store.get_consumed_carrier_leg("call_delayed_after_revoke") is None
    durable_jobs = await store.list_call_termination_jobs()
    assert durable_jobs == jobs


async def test_revoking_expired_unconsumed_admission_still_queues_parent_termination(
    store: SQLiteStore,
) -> None:
    issued_at = utc_now()
    call_sid = "CA" + ("e" * 32)
    admission = await store.issue_carrier_admission(
        call_sid,
        caller_phone="+12025550123",
        ttl_seconds=60,
        issued_at=issued_at,
    )
    revoked_at = issued_at + timedelta(seconds=61)

    jobs = await store.revoke_unconsumed_carrier_admissions(
        revoked_at=revoked_at,
    )
    replay = await store.revoke_unconsumed_carrier_admissions(
        revoked_at=revoked_at + timedelta(seconds=1),
    )

    assert [(job.leg.value, job.target_id) for job in jobs] == [("carrier", call_sid)]
    assert replay == []
    with pytest.raises(ActionExpiredError, match="expired"):
        await store.consume_carrier_admission(
            call_sid,
            caller_phone=admission.caller_phone,
            admission_nonce=admission.admission_nonce,
            expires_at_epoch=int(admission.expires_at.timestamp()),
            provider_call_id="call_delayed_after_expiry",
            consumed_at=revoked_at,
        )
    assert await store.get_consumed_carrier_leg("call_delayed_after_expiry") is None
    assert await store.list_call_termination_jobs() == jobs


async def test_revoking_expired_unconsumed_admissions_progresses_across_batches(
    store: SQLiteStore,
) -> None:
    issued_at = utc_now()
    call_sids = ["CA" + f"{index:032x}" for index in range(3)]
    for call_sid in call_sids:
        await store.issue_carrier_admission(
            call_sid,
            caller_phone="+12025550123",
            ttl_seconds=60,
            issued_at=issued_at,
        )
    revoked_at = issued_at + timedelta(seconds=61)

    first = await store.revoke_unconsumed_carrier_admissions(
        limit=2,
        revoked_at=revoked_at,
    )
    second = await store.revoke_unconsumed_carrier_admissions(
        limit=2,
        revoked_at=revoked_at,
    )
    replay = await store.revoke_unconsumed_carrier_admissions(
        limit=2,
        revoked_at=revoked_at,
    )

    assert len(first) == 2
    assert len(second) == 1
    assert replay == []
    assert {job.target_id for job in [*first, *second]} == set(call_sids)


async def test_repository_context_barrier_persists_across_store_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "taint.sqlite3"
    first = SQLiteStore(path)
    await first.initialize()
    event = event_factory()
    await first.create_event(event)
    await first.append_timeline(
        TimelineEntry(
            event_id=event.event_id,
            kind=TimelineKind.REPOSITORY_CONTEXT_EXPOSED,
        )
    )
    await first.close()

    reopened = SQLiteStore(path)
    await reopened.initialize()
    try:
        assert await reopened.repository_context_was_exposed(event.event_id) is True
        with pytest.raises(PermissionError):
            await reopened.record_decision(
                Decision(
                    event_id=event.event_id,
                    outcome="instruct",
                    instruction="Continue.",
                    identity_verified=True,
                )
            )
    finally:
        await reopened.close()


async def test_event_idempotency_active_dedupe_and_terminal_reuse(
    store: SQLiteStore,
) -> None:
    first = event_factory()
    created = await store.create_event(first)
    replay = await store.create_event(first)

    assert created.created is True
    assert replay.created is False
    assert replay.deduplicated_by == "event_id"
    assert replay.event.event_id == first.event_id

    duplicate = event_factory()
    deduped = await store.create_event(duplicate)
    assert deduped.created is False
    assert deduped.deduplicated_by == "dedupe_key"
    assert deduped.event.event_id == first.event_id

    conflicting = first.model_copy(update={"summary": "Different immutable content"})
    with pytest.raises(ConflictError):
        await store.create_event(conflicting)

    await store.transition_event(first.event_id, EventState.QUEUED)
    await store.transition_event(first.event_id, EventState.DIALING)
    await store.transition_event(first.event_id, EventState.CONNECTED)
    await store.transition_event(first.event_id, EventState.RESOLVED)

    recurrence = event_factory()
    assert (await store.create_event(recurrence)).created is True


async def test_retry_safe_state_transitions_and_timeline(store: SQLiteStore) -> None:
    event = event_factory()
    await store.create_event(event)

    queued = await store.transition_event(event.event_id, EventState.QUEUED)
    replay = await store.transition_event(event.event_id, EventState.QUEUED)
    assert queued.state is EventState.QUEUED
    assert replay.state is EventState.QUEUED

    with pytest.raises(InvalidStateTransitionError):
        await store.transition_event(event.event_id, EventState.RESOLVED)

    timeline = await store.list_timeline(event_id=event.event_id)
    assert [entry.kind for entry in timeline] == [
        TimelineKind.EVENT_CREATED,
        TimelineKind.EVENT_STATE_CHANGED,
    ]
    assert timeline[1].occurred_at >= timeline[0].occurred_at


async def test_snapshot_round_trip_is_compact_and_event_bound(store: SQLiteStore) -> None:
    event = event_factory()
    await store.create_event(event)
    snapshot = ContextSnapshot(
        event_id=event.event_id,
        agent="codex",
        task="Recover production",
        agent_summary="Capacity is exhausted; no write requests succeed.",
        diff_summary=["No code change is pending"],
        human_constraints=["Never run a migration without separate approval"],
    )

    await store.save_snapshot(snapshot)
    loaded = await store.get_snapshot(event.event_id)
    assert loaded == snapshot
    assert loaded is not None
    assert loaded.event_id == event.event_id

    missing = snapshot.model_copy(update={"case_id": new_id("evt")})
    with pytest.raises(Exception, match="does not exist"):
        await store.save_snapshot(missing)


async def test_session_exclusivity_and_provider_correlation(store: SQLiteStore) -> None:
    event = event_factory()
    await store.create_event(event)
    session = ContactSession(
        event_id=event.event_id,
        owner_ref="owner_primary",
        direction=ContactDirection.OUTBOUND_ESCALATION,
    )
    await store.create_session(session)

    with pytest.raises(ActiveSessionError):
        await store.create_session(
            ContactSession(
                event_id=event.event_id,
                owner_ref="owner_primary",
                direction=ContactDirection.OUTBOUND_ESCALATION,
            )
        )

    linked = await store.link_attempt(session.session_id, "attempt_123")
    linked = await store.link_interaction(session.session_id, "interaction_456")
    assert linked.attempt_id == "attempt_123"
    assert linked.interaction_id == "interaction_456"
    assert (await store.get_event_by_attempt("attempt_123")) == event
    assert (await store.get_event_by_interaction("interaction_456")) == event

    other_event = event_factory(dedupe_key="workspace:other:incident")
    await store.create_event(other_event)
    other_session = ContactSession(
        event_id=other_event.event_id,
        owner_ref="owner_secondary",
        direction=ContactDirection.OUTBOUND_ESCALATION,
    )
    await store.create_session(other_session)
    with pytest.raises(CorrelationError):
        await store.link_attempt(other_session.session_id, "attempt_123")


async def test_webhook_is_idempotent_and_reconciles_attempt_to_interaction(
    store: SQLiteStore,
) -> None:
    event = event_factory()
    await store.create_event(event)
    session = ContactSession(
        event_id=event.event_id,
        direction=ContactDirection.OUTBOUND_ESCALATION,
    )
    await store.create_session(session)
    await store.link_attempt(session.session_id, "attempt_123")

    webhook = ProviderWebhookPayload(
        webhook_id="webhook_123",
        attempt_id="attempt_123",
        interaction_id="interaction_456",
        status="completed",
        transcript=[
            TranscriptTurn(role=TranscriptRole.AGENT, text="Production is unavailable."),
            TranscriptTurn(role=TranscriptRole.OWNER, text="Leave it safely paused."),
        ],
        metadata={"event_id": event.event_id},
    )
    receipt = await store.record_webhook(webhook)
    replay = await store.record_webhook(webhook)

    assert receipt.created is True
    assert replay.created is False
    assert receipt.session_id == session.session_id
    updated = await store.get_session_by_interaction("interaction_456")
    assert updated is not None
    assert updated.state is SessionState.COMPLETED
    assert (await store.get_event_by_attempt("attempt_123")) == event


async def test_one_durable_decision_per_event(store: SQLiteStore) -> None:
    event = event_factory(kind="approval")
    await store.create_event(event)
    decision = Decision(
        event_id=event.event_id,
        outcome="approve",
        identity_verified=True,
    )

    recorded = await store.record_decision(decision)
    replay = await store.record_decision(decision)
    assert recorded == replay
    assert (await store.require_event(event.event_id)).state is EventState.RESOLVED

    with pytest.raises(DecisionAlreadyExistsError):
        await store.record_decision(
            Decision(
                event_id=event.event_id,
                outcome="deny",
                identity_verified=True,
            )
        )


async def test_decision_that_crosses_hard_deadline_atomically_expires_event(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = utc_now() + timedelta(minutes=1)
    event = EscalationEvent(
        kind="approval",
        summary="A decision must not arrive after this hard deadline.",
        question="May the agent continue?",
        deadline_at=deadline,
    )
    await store.create_event(event)
    decision = Decision(
        event_id=event.event_id,
        outcome="approve",
        identity_verified=True,
    )
    monkeypatch.setattr(storage_module, "utc_now", lambda: deadline)

    with pytest.raises(EventDeadlineExpiredError, match="deadline elapsed"):
        await store.record_decision(decision)

    assert await store.get_decision(event.event_id) is None
    assert (await store.require_event(event.event_id)).state is EventState.EXPIRED
    timeline = await store.list_timeline(event_id=event.event_id, limit=20)
    assert sum(item.kind is TimelineKind.DECISION_RECORDED for item in timeline) == 0
    assert (
        sum(
            item.kind is TimelineKind.EVENT_STATE_CHANGED
            and item.to_state == EventState.EXPIRED.value
            and item.details["reason"] == "decision_arrived_after_deadline"
            for item in timeline
        )
        == 1
    )


@pytest.mark.parametrize("first_operation", ["decision", "expiry"])
async def test_decision_and_background_deadline_expiry_serialize_atomically(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
    first_operation: str,
) -> None:
    deadline = utc_now() + timedelta(minutes=1)
    event = EscalationEvent(
        kind="approval",
        summary="Only one terminal deadline outcome may commit.",
        question="May this decision commit?",
        deadline_at=deadline,
    )
    await store.create_event(event)
    decision = Decision(
        event_id=event.event_id,
        outcome="deny",
        identity_verified=True,
    )
    monkeypatch.setattr(
        storage_module,
        "utc_now",
        lambda: deadline - timedelta(microseconds=1),
    )

    await store._write_lock.acquire()
    try:
        if first_operation == "decision":
            decision_task = asyncio.create_task(store.record_decision(decision))
            await asyncio.sleep(0)
            expiry_task = asyncio.create_task(store.expire_due_events(now=deadline))
        else:
            expiry_task = asyncio.create_task(store.expire_due_events(now=deadline))
            await asyncio.sleep(0)
            decision_task = asyncio.create_task(store.record_decision(decision))
        await asyncio.sleep(0)
    finally:
        store._write_lock.release()

    if first_operation == "decision":
        assert await decision_task == decision
        assert await expiry_task == []
        assert await store.get_decision(event.event_id) == decision
        expected_state = EventState.RESOLVED
    else:
        assert await expiry_task == [event.event_id]
        with pytest.raises(InvalidStateTransitionError, match="expired event"):
            await decision_task
        assert await store.get_decision(event.event_id) is None
        expected_state = EventState.EXPIRED

    assert (await store.require_event(event.event_id)).state is expected_state
    timeline = await store.list_timeline(event_id=event.event_id, limit=20)
    terminal_transitions = [
        item
        for item in timeline
        if item.kind is TimelineKind.EVENT_STATE_CHANGED
        and item.to_state in {EventState.RESOLVED.value, EventState.EXPIRED.value}
    ]
    assert len(terminal_transitions) == 1


async def test_active_only_provider_link_rejects_expired_event_without_mutation(
    store: SQLiteStore,
) -> None:
    now = utc_now()
    deadline = now + timedelta(minutes=1)
    event = EscalationEvent(
        kind="incident",
        summary="A delayed placement result crossed the hard deadline.",
        deadline_at=deadline,
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
        )
    )
    assert await store.expire_due_events(now=deadline) == [event.event_id]

    with pytest.raises(InvalidStateTransitionError, match="event is no longer active"):
        await store.link_attempt(
            session.session_id,
            "attempt_after_hard_deadline",
            occurred_at=deadline,
            require_active=True,
        )

    persisted = await store.get_session(session.session_id)
    assert persisted is not None
    assert persisted.attempt_id is None
    assert persisted.state is SessionState.DIALING
    timeline = await store.list_timeline(event_id=event.event_id, limit=20)
    assert all(item.kind is not TimelineKind.ATTEMPT_LINKED for item in timeline)


async def test_action_confirmation_and_atomic_one_time_consumption(
    store: SQLiteStore,
) -> None:
    event = event_factory(kind="approval")
    await store.create_event(event)
    session = ContactSession(
        event_id=event.event_id,
        direction=ContactDirection.OUTBOUND_ESCALATION,
    )
    await store.create_session(session)
    confirmation_hash = digest("confirm production shutdown")
    action_hash = digest("registered_runbook:shutdown:production")
    action = PreparedAction(
        event_id=event.event_id,
        session_id=session.session_id,
        kind=ActionKind.REGISTERED_RUNBOOK,
        target="shutdown api-service production",
        scope=ActionScope(
            host_id="local",
            workspace="repo_primary",
            environment="production",
        ),
        risk=RiskLevel.CRITICAL,
        action_hash=action_hash,
        confirmation_phrase_hash=confirmation_hash,
        expires_at=utc_now() + timedelta(minutes=5),
    )
    prepared = await store.prepare_action(action)
    assert prepared.state is ActionState.PREPARED

    with pytest.raises(ActionHashMismatchError):
        await store.confirm_action(
            action.action_id,
            owner_ref="owner_primary",
            confirmation_method=ConfirmationMethod.SPOKEN_PHRASE,
            confirmation_hash=digest("wrong phrase"),
        )

    grant = await store.confirm_action(
        action.action_id,
        owner_ref="owner_primary",
        confirmation_method=ConfirmationMethod.SPOKEN_PHRASE,
        confirmation_hash=confirmation_hash,
    )
    grant_replay = await store.confirm_action(
        action.action_id,
        owner_ref="owner_primary",
        confirmation_method=ConfirmationMethod.SPOKEN_PHRASE,
        confirmation_hash=confirmation_hash,
    )
    assert grant_replay.grant_id == grant.grant_id

    results = await asyncio.gather(
        store.consume_action(grant.grant_id, action_hash=action_hash),
        store.consume_action(grant.grant_id, action_hash=action_hash),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert sum(isinstance(result, ActionAlreadyConsumedError) for result in results) == 1
    assert (await store.get_action(action.action_id)).state is ActionState.CONSUMED


async def test_action_execution_receipts_are_terminal_and_idempotent(
    store: SQLiteStore,
) -> None:
    event = event_factory(kind="approval")
    await store.create_event(event)
    cases = (
        (True, TimelineKind.ACTION_EXECUTION_SUCCEEDED, "succeeded"),
        (False, TimelineKind.ACTION_EXECUTION_FAILED, "failed"),
        (None, TimelineKind.ACTION_EXECUTION_UNKNOWN, "unknown"),
    )

    for index, (succeeded, expected_kind, expected_status) in enumerate(cases):
        action = PreparedAction(
            event_id=event.event_id,
            kind=ActionKind.REGISTERED_RUNBOOK,
            target=f"demo.action_{index}",
            scope=ActionScope(host_id="local"),
            risk=RiskLevel.HIGH,
            action_hash=digest(f"action-execution-{index}"),
            confirmation_phrase_hash=digest(f"confirm-action-{index}"),
            expires_at=utc_now() + timedelta(minutes=5),
        )
        await store.prepare_action(action)
        first = await store.record_action_execution(
            action.action_id,
            succeeded=succeeded,
            message_to_user=f"Terminal {expected_status} result.",
            operation_id=f"operation-{index}",
            result={"case": index},
            retryable=False,
        )
        replay = await store.record_action_execution(
            action.action_id,
            succeeded=not succeeded if succeeded is not None else True,
            message_to_user="Conflicting later outcome must not replace the first receipt.",
            retryable=True,
        )

        assert replay == first
        assert await store.get_action_execution(action.action_id) == first
        assert first.kind is expected_kind
        assert first.details == {
            "status": expected_status,
            "message_to_user": f"Terminal {expected_status} result.",
            "result": {"case": index},
            "retryable": False,
            "operation_id": f"operation-{index}",
        }


async def test_expired_identical_action_does_not_shadow_a_fresh_prepare(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = event_factory(kind="approval")
    await store.create_event(event)
    first_now = utc_now()
    monkeypatch.setattr(storage_module, "utc_now", lambda: first_now)
    common = {
        "event_id": event.event_id,
        "kind": ActionKind.REGISTERED_RUNBOOK,
        "target": "demo.pause_deployment",
        "scope": ActionScope(host_id="local"),
        "risk": RiskLevel.HIGH,
        "action_hash": digest("identical-expiring-action"),
        "confirmation_phrase_hash": digest("confirm-identical-action"),
    }
    expired = PreparedAction(
        **common,
        created_at=first_now,
        expires_at=first_now + timedelta(seconds=1),
    )
    await store.prepare_action(expired)

    retry_now = first_now + timedelta(seconds=2)
    monkeypatch.setattr(storage_module, "utc_now", lambda: retry_now)
    fresh = PreparedAction(
        **common,
        created_at=retry_now,
        expires_at=retry_now + timedelta(minutes=5),
    )
    prepared = await store.prepare_action(fresh)

    assert prepared.action_id == fresh.action_id
    assert (await store.get_action(expired.action_id)).state is ActionState.EXPIRED
    assert (await store.get_action(fresh.action_id)).state is ActionState.PREPARED


async def test_expired_grant_is_persistently_unusable(store: SQLiteStore) -> None:
    event = event_factory(kind="approval")
    await store.create_event(event)
    created_at = utc_now()
    action = PreparedAction(
        event_id=event.event_id,
        kind=ActionKind.INTERRUPT_THREAD,
        target="training-run",
        scope=ActionScope(host_id="local", thread_id="thread_training"),
        risk=RiskLevel.HIGH,
        action_hash=digest("interrupt:training-run"),
        confirmation_phrase_hash=digest("confirm interrupt"),
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=1),
    )
    await store.prepare_action(action)

    with pytest.raises(ActionExpiredError):
        await store.confirm_action(
            action.action_id,
            owner_ref="owner_primary",
            confirmation_method=ConfirmationMethod.SPOKEN_PHRASE,
            confirmation_hash=digest("confirm interrupt"),
            now=created_at + timedelta(seconds=2),
        )
    assert (await store.get_action(action.action_id)).state is ActionState.EXPIRED


async def test_persists_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "durable.sqlite3"
    event = event_factory()
    first = SQLiteStore(path)
    await first.initialize()
    await first.create_event(event)
    await first.close()

    second = SQLiteStore(path)
    await second.initialize()
    try:
        assert await second.get_event(event.event_id) == event
    finally:
        await second.close()
