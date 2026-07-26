from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio

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
    RiskLevel,
    SarvamWebhookPayload,
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
    assert await store.pragma("user_version") == 2


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

    webhook = SarvamWebhookPayload(
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
