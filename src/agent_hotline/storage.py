"""Async SQLite persistence for Agent Hotline.

The store uses explicit ``BEGIN IMMEDIATE`` transactions for every mutation.
That makes decision uniqueness, provider correlation, and one-time action grant
consumption durable across process restarts and safe under concurrent callers.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel

from agent_hotline.models import (
    EVENT_STATE_TRANSITIONS,
    SESSION_STATE_TRANSITIONS,
    TERMINAL_EVENT_STATES,
    TERMINAL_SESSION_STATES,
    ActionGrant,
    ActionState,
    ConfirmationMethod,
    ContactSession,
    ContextSnapshot,
    Decision,
    EscalationEvent,
    EventCreateResult,
    EventState,
    FallbackLink,
    FallbackState,
    GrantState,
    PreparedAction,
    SarvamWebhookPayload,
    SessionState,
    TimelineEntry,
    TimelineKind,
    WebhookReceipt,
    WebhookStatus,
    canonical_model_json,
    utc_now,
    validate_owner_ref,
)


class StorageError(RuntimeError):
    """Base class for durable-store failures."""


class StoreNotInitializedError(StorageError):
    """Raised when an operation is attempted before ``initialize``."""


class NotFoundError(StorageError):
    """Raised when a requested durable object does not exist."""


class ConflictError(StorageError):
    """Raised when an idempotency key is reused for different content."""


class InvalidStateTransitionError(StorageError):
    """Raised when a state machine transition is not allowed."""


class ActiveSessionError(ConflictError):
    """Raised when an owner already has an active contact session."""


class DecisionAlreadyExistsError(ConflictError):
    """Raised when an event already has a different durable decision."""


class CorrelationError(ConflictError):
    """Raised when provider identifiers cannot be correlated safely."""


class ActionExpiredError(StorageError):
    """Raised when an action or grant has expired."""


class ActionAlreadyConsumedError(ConflictError):
    """Raised when a one-time action grant is replayed."""


class ActionHashMismatchError(ConflictError):
    """Raised when confirmation or consumption is for different action bytes."""


class FallbackLinkError(StorageError):
    """Raised when a secure fallback link is invalid, expired, or unavailable."""


class FallbackAlreadyConsumedError(ConflictError):
    """Raised when a one-time fallback submission is replayed."""


_SCHEMA_VERSION = 2
_ACTIVE_EVENT_VALUES = (
    EventState.DETECTED.value,
    EventState.QUEUED.value,
    EventState.DIALING.value,
    EventState.CONNECTED.value,
    EventState.AWAITING_DECISION.value,
    EventState.FALLBACK_PENDING.value,
)
_ACTIVE_SESSION_VALUES = (
    SessionState.PENDING.value,
    SessionState.DIALING.value,
    SessionState.RINGING.value,
    SessionState.CONNECTED.value,
    SessionState.DISCUSSING.value,
)
_ACTIVE_ACTION_VALUES = (ActionState.PREPARED.value, ActionState.CONFIRMED.value)

_DDL = f"""
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL,
    ingest_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    deadline_at TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_active_dedupe_v2
ON events(dedupe_key)
WHERE state IN ({",".join(repr(value) for value in _ACTIVE_EVENT_VALUES)});

CREATE INDEX IF NOT EXISTS ix_events_detected_at
ON events(detected_at DESC);

CREATE TABLE IF NOT EXISTS context_snapshots (
    event_id TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
    payload_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS contact_sessions (
    session_id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    owner_ref TEXT NOT NULL,
    direction TEXT NOT NULL,
    channel TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_id TEXT UNIQUE,
    interaction_id TEXT UNIQUE,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_active_owner
ON contact_sessions(owner_ref)
WHERE state IN ({",".join(repr(value) for value in _ACTIVE_SESSION_VALUES)});

CREATE INDEX IF NOT EXISTS ix_sessions_event
ON contact_sessions(event_id, started_at DESC);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT,
    session_id TEXT REFERENCES contact_sessions(session_id) ON DELETE RESTRICT,
    payload_json TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS provider_webhooks (
    webhook_key TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    interaction_id TEXT,
    event_id TEXT REFERENCES events(event_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES contact_sessions(session_id) ON DELETE RESTRICT,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_webhooks_attempt
ON provider_webhooks(attempt_id, received_at DESC);

CREATE TABLE IF NOT EXISTS prepared_actions (
    action_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    session_id TEXT REFERENCES contact_sessions(session_id) ON DELETE RESTRICT,
    action_hash TEXT NOT NULL,
    confirmation_phrase_hash TEXT,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX IF NOT EXISTS ux_actions_active_hash
ON prepared_actions(action_hash)
WHERE state IN ({",".join(repr(value) for value in _ACTIVE_ACTION_VALUES)});

CREATE INDEX IF NOT EXISTS ix_actions_expiry
ON prepared_actions(state, expires_at);

CREATE TABLE IF NOT EXISTS action_grants (
    grant_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL UNIQUE REFERENCES prepared_actions(action_id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON DELETE RESTRICT,
    session_id TEXT REFERENCES contact_sessions(session_id) ON DELETE RESTRICT,
    action_hash TEXT NOT NULL,
    owner_ref TEXT NOT NULL,
    state TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_grants_expiry
ON action_grants(state, expires_at);

CREATE TABLE IF NOT EXISTS fallback_links (
    fallback_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT,
    session_id TEXT NOT NULL REFERENCES contact_sessions(session_id) ON DELETE RESTRICT,
    state TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_fallback_links_expiry
ON fallback_links(state, expires_at);

CREATE TABLE IF NOT EXISTS timeline (
    timeline_id TEXT PRIMARY KEY,
    event_id TEXT REFERENCES events(event_id) ON DELETE CASCADE,
    session_id TEXT REFERENCES contact_sessions(session_id) ON DELETE CASCADE,
    action_id TEXT REFERENCES prepared_actions(action_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    details_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS ix_timeline_event_time
ON timeline(event_id, occurred_at, timeline_id);

CREATE INDEX IF NOT EXISTS ix_timeline_session_time
ON timeline(session_id, occurred_at, timeline_id);
"""


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _load[ModelT: BaseModel](model_type: type[ModelT], payload: str) -> ModelT:
    return model_type.model_validate_json(payload)


def _fingerprint(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    payload = canonical_model_json(model, exclude=exclude)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SQLiteStore:
    """Single-process async store backed by durable SQLite WAL."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self._connection: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise StoreNotInitializedError("call initialize() before using the store")
        return self._connection

    async def initialize(self) -> None:
        """Open the database, enable WAL/foreign keys, and install the schema."""

        if self._connection is not None:
            return
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(
            str(self.path),
            isolation_level=None,
            timeout=5.0,
        )
        connection.row_factory = aiosqlite.Row
        try:
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute("PRAGMA busy_timeout = 5000")
            await connection.execute("PRAGMA synchronous = FULL")
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.executescript(_DDL)
            await connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            await connection.close()
            raise
        self._connection = connection

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            await connection.close()

    async def __aenter__(self) -> SQLiteStore:
        await self.initialize()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def pragma(self, name: str) -> Any:
        """Read a safe, allowlisted PRAGMA value (primarily for diagnostics)."""

        if name not in {"foreign_keys", "journal_mode", "synchronous", "user_version"}:
            raise ValueError("unsupported PRAGMA")
        async with self._write_lock:
            cursor = await self.connection.execute(f"PRAGMA {name}")
            row = await cursor.fetchone()
        return None if row is None else row[0]

    async def _begin(self) -> None:
        await self.connection.execute("BEGIN IMMEDIATE")

    async def _commit(self) -> None:
        await self.connection.commit()

    async def _rollback(self) -> None:
        if self.connection.in_transaction:
            await self.connection.rollback()

    async def _fetch_event_locked(self, event_id: str) -> EscalationEvent | None:
        cursor = await self.connection.execute(
            "SELECT payload_json FROM events WHERE event_id = ?",
            (event_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else _load(EscalationEvent, row["payload_json"])

    async def _fetch_session_locked(self, session_id: str) -> ContactSession | None:
        cursor = await self.connection.execute(
            "SELECT payload_json FROM contact_sessions WHERE session_id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else _load(ContactSession, row["payload_json"])

    async def _fetch_action_locked(self, action_id: str) -> PreparedAction | None:
        cursor = await self.connection.execute(
            "SELECT payload_json FROM prepared_actions WHERE action_id = ?",
            (action_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else _load(PreparedAction, row["payload_json"])

    async def _fetch_grant_locked(self, grant_id: str) -> ActionGrant | None:
        cursor = await self.connection.execute(
            "SELECT payload_json FROM action_grants WHERE grant_id = ?",
            (grant_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else _load(ActionGrant, row["payload_json"])

    async def _fetch_fallback_locked(self, fallback_id: str) -> FallbackLink | None:
        cursor = await self.connection.execute(
            "SELECT payload_json FROM fallback_links WHERE fallback_id = ?",
            (fallback_id,),
        )
        row = await cursor.fetchone()
        return None if row is None else _load(FallbackLink, row["payload_json"])

    async def _repository_context_was_exposed_locked(self, event_id: str) -> bool:
        cursor = await self.connection.execute(
            """
            SELECT 1
            FROM timeline
            WHERE event_id = ? AND kind = ?
            LIMIT 1
            """,
            (event_id, TimelineKind.REPOSITORY_CONTEXT_EXPOSED.value),
        )
        return await cursor.fetchone() is not None

    async def repository_context_was_exposed(self, event_id: str) -> bool:
        async with self._write_lock:
            return await self._repository_context_was_exposed_locked(event_id)

    async def _require_no_repository_context_locked(self, event_id: str) -> None:
        """Keep untrusted repository evidence separate from authority-bearing tools."""

        if await self._repository_context_was_exposed_locked(event_id):
            raise PermissionError(
                "repository evidence was exposed in this event; "
                "use a fresh confirmation call for decisions or actions"
            )

    async def _insert_timeline_locked(self, entry: TimelineEntry) -> None:
        await self.connection.execute(
            """
            INSERT INTO timeline (
                timeline_id, event_id, session_id, action_id, kind,
                from_state, to_state, details_json, occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.timeline_id,
                entry.event_id,
                entry.session_id,
                entry.action_id,
                entry.kind.value,
                entry.from_state,
                entry.to_state,
                json.dumps(
                    entry.details,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
                _iso(entry.occurred_at),
                canonical_model_json(entry),
            ),
        )

    async def append_timeline(self, entry: TimelineEntry) -> TimelineEntry:
        async with self._write_lock:
            await self._begin()
            try:
                await self._insert_timeline_locked(entry)
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return entry

    async def create_event(self, event: EscalationEvent) -> EventCreateResult:
        """Idempotently ingest an event and suppress an active dedupe collision."""

        if event.state is not EventState.DETECTED:
            raise ValueError("new events must start in the detected state")
        assert event.dedupe_key is not None
        payload = canonical_model_json(event)
        ingest_fingerprint = _fingerprint(event, exclude={"state"})
        now = utc_now()

        async with self._write_lock:
            await self._begin()
            try:
                try:
                    await self.connection.execute(
                        """
                        INSERT INTO events (
                            event_id, dedupe_key, ingest_fingerprint, state,
                            detected_at, deadline_at, payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event.event_id,
                            event.dedupe_key,
                            ingest_fingerprint,
                            event.state.value,
                            _iso(event.detected_at),
                            _iso(event.deadline_at) if event.deadline_at else None,
                            payload,
                            _iso(now),
                            _iso(now),
                        ),
                    )
                except sqlite3.IntegrityError:
                    cursor = await self.connection.execute(
                        """
                        SELECT payload_json, ingest_fingerprint
                        FROM events
                        WHERE event_id = ?
                        """,
                        (event.event_id,),
                    )
                    row = await cursor.fetchone()
                    if row is not None:
                        if not hmac.compare_digest(row["ingest_fingerprint"], ingest_fingerprint):
                            raise ConflictError(
                                f"event_id {event.event_id!r} was reused with different content"
                            ) from None
                        existing = _load(EscalationEvent, row["payload_json"])
                        result = EventCreateResult(
                            event=existing,
                            created=False,
                            deduplicated_by="event_id",
                        )
                    else:
                        placeholders = ",".join("?" for _ in _ACTIVE_EVENT_VALUES)
                        cursor = await self.connection.execute(
                            f"""
                            SELECT payload_json
                            FROM events
                            WHERE dedupe_key = ? AND state IN ({placeholders})
                            ORDER BY detected_at DESC
                            LIMIT 1
                            """,
                            (event.dedupe_key, *_ACTIVE_EVENT_VALUES),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise
                        result = EventCreateResult(
                            event=_load(EscalationEvent, row["payload_json"]),
                            created=False,
                            deduplicated_by="dedupe_key",
                        )
                else:
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=event.event_id,
                            kind=TimelineKind.EVENT_CREATED,
                            to_state=event.state.value,
                            occurred_at=event.detected_at,
                        )
                    )
                    result = EventCreateResult(event=event, created=True)
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return result

    async def get_event(self, event_id: str) -> EscalationEvent | None:
        async with self._write_lock:
            return await self._fetch_event_locked(event_id)

    async def require_event(self, event_id: str) -> EscalationEvent:
        event = await self.get_event(event_id)
        if event is None:
            raise NotFoundError(f"event {event_id!r} does not exist")
        return event

    async def list_events(
        self,
        *,
        limit: int = 50,
        states: Iterable[EventState] | None = None,
    ) -> list[EscalationEvent]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        state_values = tuple(state.value for state in states) if states is not None else ()
        query = "SELECT payload_json FROM events"
        parameters: list[Any] = []
        if state_values:
            placeholders = ",".join("?" for _ in state_values)
            query += f" WHERE state IN ({placeholders})"
            parameters.extend(state_values)
        query += " ORDER BY detected_at DESC, event_id DESC LIMIT ?"
        parameters.append(limit)
        async with self._write_lock:
            cursor = await self.connection.execute(query, parameters)
            rows = await cursor.fetchall()
        return [_load(EscalationEvent, row["payload_json"]) for row in rows]

    async def transition_event(
        self,
        event_id: str,
        to_state: EventState | str,
        *,
        occurred_at: datetime | None = None,
        details: dict[str, Any] | None = None,
    ) -> EscalationEvent:
        to_state = EventState(to_state)
        timestamp = _as_utc(occurred_at)
        safe_details = details or {}
        # TimelineEntry performs secret/phone/JSON validation.
        timeline = TimelineEntry(
            event_id=event_id,
            kind=TimelineKind.EVENT_STATE_CHANGED,
            details=safe_details,
            occurred_at=timestamp,
        )
        async with self._write_lock:
            await self._begin()
            try:
                event = await self._fetch_event_locked(event_id)
                if event is None:
                    raise NotFoundError(f"event {event_id!r} does not exist")
                if event.state is to_state:
                    await self._commit()
                    return event
                if to_state not in EVENT_STATE_TRANSITIONS[event.state]:
                    raise InvalidStateTransitionError(
                        f"event transition {event.state.value!r} -> {to_state.value!r} is invalid"
                    )
                updated = event.model_copy(update={"state": to_state})
                await self.connection.execute(
                    """
                    UPDATE events
                    SET state = ?, payload_json = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (
                        to_state.value,
                        canonical_model_json(updated),
                        _iso(timestamp),
                        event_id,
                    ),
                )
                await self._insert_timeline_locked(
                    timeline.model_copy(
                        update={
                            "from_state": event.state.value,
                            "to_state": to_state.value,
                        }
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return updated

    async def save_snapshot(self, snapshot: ContextSnapshot) -> ContextSnapshot:
        payload = canonical_model_json(snapshot)
        now = utc_now()
        async with self._write_lock:
            await self._begin()
            try:
                if await self._fetch_event_locked(snapshot.event_id) is None:
                    raise NotFoundError(f"event {snapshot.event_id!r} does not exist")
                cursor = await self.connection.execute(
                    "SELECT payload_json FROM context_snapshots WHERE event_id = ?",
                    (snapshot.event_id,),
                )
                existing = await cursor.fetchone()
                if existing is not None and hmac.compare_digest(existing["payload_json"], payload):
                    await self._commit()
                    return snapshot
                await self.connection.execute(
                    """
                    INSERT INTO context_snapshots (
                        event_id, payload_json, captured_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        captured_at = excluded.captured_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        snapshot.event_id,
                        payload,
                        _iso(snapshot.captured_at),
                        _iso(now),
                    ),
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=snapshot.event_id,
                        kind=TimelineKind.SNAPSHOT_SAVED,
                        occurred_at=now,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return snapshot

    async def get_snapshot(self, event_id: str) -> ContextSnapshot | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM context_snapshots WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(ContextSnapshot, row["payload_json"])

    async def create_session(self, session: ContactSession) -> ContactSession:
        payload = canonical_model_json(session)
        now = utc_now()
        async with self._write_lock:
            await self._begin()
            try:
                if (
                    session.event_id is not None
                    and await self._fetch_event_locked(session.event_id) is None
                ):
                    raise NotFoundError(f"event {session.event_id!r} does not exist")
                try:
                    await self.connection.execute(
                        """
                        INSERT INTO contact_sessions (
                            session_id, event_id, owner_ref, direction, channel, state,
                            attempt_id, interaction_id, started_at, ended_at,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session.session_id,
                            session.event_id,
                            session.owner_ref,
                            session.direction.value,
                            session.channel.value,
                            session.state.value,
                            session.attempt_id,
                            session.interaction_id,
                            _iso(session.started_at),
                            _iso(session.ended_at) if session.ended_at else None,
                            payload,
                            _iso(now),
                            _iso(now),
                        ),
                    )
                except sqlite3.IntegrityError:
                    existing = await self._fetch_session_locked(session.session_id)
                    if existing is not None:
                        if not hmac.compare_digest(canonical_model_json(existing), payload):
                            raise ConflictError(
                                f"session_id {session.session_id!r} was reused with "
                                "different content"
                            ) from None
                        await self._commit()
                        return existing
                    placeholders = ",".join("?" for _ in _ACTIVE_SESSION_VALUES)
                    cursor = await self.connection.execute(
                        f"""
                        SELECT session_id
                        FROM contact_sessions
                        WHERE owner_ref = ? AND state IN ({placeholders})
                        LIMIT 1
                        """,
                        (session.owner_ref, *_ACTIVE_SESSION_VALUES),
                    )
                    active = await cursor.fetchone()
                    if active is not None:
                        raise ActiveSessionError(
                            f"owner already has active session {active['session_id']!r}"
                        ) from None
                    raise ConflictError(
                        "attempt_id or interaction_id is already linked to another session"
                    ) from None
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=session.event_id,
                        session_id=session.session_id,
                        kind=TimelineKind.SESSION_CREATED,
                        to_state=session.state.value,
                        occurred_at=session.started_at,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return session

    async def get_session(self, session_id: str) -> ContactSession | None:
        async with self._write_lock:
            return await self._fetch_session_locked(session_id)

    async def list_sessions(
        self,
        *,
        event_id: str | None = None,
        limit: int = 50,
    ) -> list[ContactSession]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query = "SELECT payload_json FROM contact_sessions"
        parameters: list[Any] = []
        if event_id is not None:
            query += " WHERE event_id = ?"
            parameters.append(event_id)
        query += " ORDER BY started_at DESC, session_id DESC LIMIT ?"
        parameters.append(limit)
        async with self._write_lock:
            cursor = await self.connection.execute(query, parameters)
            rows = await cursor.fetchall()
        return [_load(ContactSession, row["payload_json"]) for row in rows]

    async def get_session_by_attempt(self, attempt_id: str) -> ContactSession | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM contact_sessions WHERE attempt_id = ?",
                (attempt_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(ContactSession, row["payload_json"])

    session_by_attempt = get_session_by_attempt

    async def get_session_by_interaction(self, interaction_id: str) -> ContactSession | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM contact_sessions WHERE interaction_id = ?",
                (interaction_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(ContactSession, row["payload_json"])

    async def get_event_by_attempt(self, attempt_id: str) -> EscalationEvent | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                """
                SELECT e.payload_json
                FROM events AS e
                JOIN contact_sessions AS s ON s.event_id = e.event_id
                WHERE s.attempt_id = ?
                """,
                (attempt_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(EscalationEvent, row["payload_json"])

    event_by_attempt = get_event_by_attempt

    async def get_event_by_interaction(self, interaction_id: str) -> EscalationEvent | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                """
                SELECT e.payload_json
                FROM events AS e
                JOIN contact_sessions AS s ON s.event_id = e.event_id
                WHERE s.interaction_id = ?
                """,
                (interaction_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(EscalationEvent, row["payload_json"])

    async def _replace_session_locked(
        self,
        session: ContactSession,
        *,
        timestamp: datetime,
    ) -> None:
        await self.connection.execute(
            """
            UPDATE contact_sessions
            SET state = ?, attempt_id = ?, interaction_id = ?, ended_at = ?,
                payload_json = ?, updated_at = ?
            WHERE session_id = ?
            """,
            (
                session.state.value,
                session.attempt_id,
                session.interaction_id,
                _iso(session.ended_at) if session.ended_at else None,
                canonical_model_json(session),
                _iso(timestamp),
                session.session_id,
            ),
        )

    async def link_attempt(
        self,
        session_id: str,
        attempt_id: str,
        *,
        occurred_at: datetime | None = None,
    ) -> ContactSession:
        timestamp = _as_utc(occurred_at)
        async with self._write_lock:
            await self._begin()
            try:
                session = await self._fetch_session_locked(session_id)
                if session is None:
                    raise NotFoundError(f"session {session_id!r} does not exist")
                if session.attempt_id == attempt_id:
                    await self._commit()
                    return session
                if session.attempt_id is not None:
                    raise CorrelationError(
                        f"session is already linked to attempt {session.attempt_id!r}"
                    )
                updated = session.model_copy(update={"attempt_id": attempt_id})
                try:
                    await self._replace_session_locked(updated, timestamp=timestamp)
                except sqlite3.IntegrityError:
                    raise CorrelationError(
                        f"attempt {attempt_id!r} is already linked to another session"
                    ) from None
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=session.event_id,
                        session_id=session_id,
                        kind=TimelineKind.ATTEMPT_LINKED,
                        details={"attempt_id": attempt_id},
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return updated

    async def link_interaction(
        self,
        session_id: str,
        interaction_id: str,
        *,
        occurred_at: datetime | None = None,
    ) -> ContactSession:
        timestamp = _as_utc(occurred_at)
        async with self._write_lock:
            await self._begin()
            try:
                session = await self._fetch_session_locked(session_id)
                if session is None:
                    raise NotFoundError(f"session {session_id!r} does not exist")
                if session.interaction_id == interaction_id:
                    await self._commit()
                    return session
                if session.interaction_id is not None:
                    raise CorrelationError(
                        f"session is already linked to interaction {session.interaction_id!r}"
                    )
                updated = session.model_copy(update={"interaction_id": interaction_id})
                try:
                    await self._replace_session_locked(updated, timestamp=timestamp)
                except sqlite3.IntegrityError:
                    raise CorrelationError(
                        f"interaction {interaction_id!r} is already linked to another session"
                    ) from None
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=session.event_id,
                        session_id=session_id,
                        kind=TimelineKind.INTERACTION_LINKED,
                        details={"interaction_id": interaction_id},
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return updated

    async def transition_session(
        self,
        session_id: str,
        to_state: SessionState | str,
        *,
        occurred_at: datetime | None = None,
        failure_reason: str | None = None,
    ) -> ContactSession:
        to_state = SessionState(to_state)
        timestamp = _as_utc(occurred_at)
        async with self._write_lock:
            await self._begin()
            try:
                session = await self._fetch_session_locked(session_id)
                if session is None:
                    raise NotFoundError(f"session {session_id!r} does not exist")
                if session.state is to_state:
                    await self._commit()
                    return session
                if to_state not in SESSION_STATE_TRANSITIONS[session.state]:
                    raise InvalidStateTransitionError(
                        f"session transition {session.state.value!r} -> "
                        f"{to_state.value!r} is invalid"
                    )
                changes: dict[str, Any] = {"state": to_state}
                if to_state is SessionState.CONNECTED and session.answered_at is None:
                    changes["answered_at"] = timestamp
                if to_state in TERMINAL_SESSION_STATES and session.ended_at is None:
                    changes["ended_at"] = timestamp
                if failure_reason is not None:
                    changes["failure_reason"] = failure_reason
                updated = ContactSession.model_validate(
                    session.model_copy(update=changes).model_dump()
                )
                await self._replace_session_locked(updated, timestamp=timestamp)
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=session.event_id,
                        session_id=session_id,
                        kind=TimelineKind.SESSION_STATE_CHANGED,
                        from_state=session.state.value,
                        to_state=to_state.value,
                        details={"failure_reason": failure_reason} if failure_reason else {},
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return updated

    async def record_decision(self, decision: Decision) -> Decision:
        """Persist the event's only decision and atomically resolve the event.

        A retry with the same decision ID and bytes is idempotent.  Any other
        second decision for the event is rejected, so a completion webhook can
        never overwrite a decision recorded by the live conversation tool.
        """

        payload = canonical_model_json(decision)
        timestamp = decision.decided_at
        async with self._write_lock:
            await self._begin()
            try:
                event = await self._fetch_event_locked(decision.event_id)
                if event is None:
                    raise NotFoundError(f"event {decision.event_id!r} does not exist")
                if decision.session_id is not None:
                    session = await self._fetch_session_locked(decision.session_id)
                    if session is None:
                        raise NotFoundError(f"session {decision.session_id!r} does not exist")
                    if session.event_id != decision.event_id:
                        raise CorrelationError(
                            "decision session does not belong to the decision event"
                        )
                cursor = await self.connection.execute(
                    "SELECT decision_id, payload_json FROM decisions WHERE event_id = ?",
                    (decision.event_id,),
                )
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    existing = _load(Decision, existing_row["payload_json"])
                    if existing.decision_id == decision.decision_id and hmac.compare_digest(
                        existing_row["payload_json"], payload
                    ):
                        await self._commit()
                        return existing
                    raise DecisionAlreadyExistsError(
                        f"event {decision.event_id!r} already has decision "
                        f"{existing_row['decision_id']!r}"
                    )
                await self._require_no_repository_context_locked(decision.event_id)
                if event.state in {EventState.EXPIRED, EventState.FAILED}:
                    raise InvalidStateTransitionError(
                        f"cannot record a decision for {event.state.value} event"
                    )
                await self.connection.execute(
                    """
                    INSERT INTO decisions (
                        decision_id, event_id, session_id, payload_json, decided_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.event_id,
                        decision.session_id,
                        payload,
                        _iso(decision.decided_at),
                        _iso(utc_now()),
                    ),
                )
                if event.state is not EventState.RESOLVED:
                    resolved = event.model_copy(update={"state": EventState.RESOLVED})
                    await self.connection.execute(
                        """
                        UPDATE events
                        SET state = ?, payload_json = ?, updated_at = ?
                        WHERE event_id = ?
                        """,
                        (
                            EventState.RESOLVED.value,
                            canonical_model_json(resolved),
                            _iso(timestamp),
                            decision.event_id,
                        ),
                    )
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=decision.event_id,
                            session_id=decision.session_id,
                            kind=TimelineKind.EVENT_STATE_CHANGED,
                            from_state=event.state.value,
                            to_state=EventState.RESOLVED.value,
                            details={"reason": "decision_recorded"},
                            occurred_at=timestamp,
                        )
                    )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=decision.event_id,
                        session_id=decision.session_id,
                        kind=TimelineKind.DECISION_RECORDED,
                        details={
                            "decision_id": decision.decision_id,
                            "outcome": decision.outcome.value,
                        },
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return decision

    async def get_decision(self, event_id: str) -> Decision | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM decisions WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(Decision, row["payload_json"])

    async def get_decision_by_id(self, decision_id: str) -> Decision | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM decisions WHERE decision_id = ?",
                (decision_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(Decision, row["payload_json"])

    async def record_webhook(
        self,
        payload: SarvamWebhookPayload,
        *,
        received_at: datetime | None = None,
    ) -> WebhookReceipt:
        """Idempotently persist and reconcile a normalized completion webhook."""

        received = _as_utc(received_at)
        payload_json = canonical_model_json(payload)
        webhook_key = payload.webhook_id or (
            "whk_" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        )
        async with self._write_lock:
            await self._begin()
            try:
                cursor = await self.connection.execute(
                    """
                    SELECT event_id, session_id, status
                    FROM provider_webhooks
                    WHERE webhook_key = ?
                    """,
                    (webhook_key,),
                )
                duplicate = await cursor.fetchone()
                if duplicate is not None:
                    await self._commit()
                    return WebhookReceipt(
                        webhook_key=webhook_key,
                        event_id=duplicate["event_id"],
                        session_id=duplicate["session_id"],
                        created=False,
                        status=WebhookStatus(duplicate["status"]),
                    )

                cursor = await self.connection.execute(
                    """
                    SELECT payload_json
                    FROM contact_sessions
                    WHERE attempt_id = ?
                    """,
                    (payload.attempt_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    metadata_event_id = payload.metadata.get("event_id")
                    if not isinstance(metadata_event_id, str):
                        raise CorrelationError(
                            f"webhook attempt {payload.attempt_id!r} is not linked"
                        )
                    cursor = await self.connection.execute(
                        """
                        SELECT payload_json
                        FROM contact_sessions
                        WHERE event_id = ? AND attempt_id IS NULL
                        ORDER BY started_at DESC
                        LIMIT 1
                        """,
                        (metadata_event_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise CorrelationError(
                            f"webhook attempt {payload.attempt_id!r} is not linked"
                        )
                    session = _load(ContactSession, row["payload_json"])
                    session = session.model_copy(update={"attempt_id": payload.attempt_id})
                else:
                    session = _load(ContactSession, row["payload_json"])

                if payload.interaction_id is not None:
                    cursor = await self.connection.execute(
                        """
                        SELECT session_id
                        FROM contact_sessions
                        WHERE interaction_id = ? AND session_id != ?
                        """,
                        (payload.interaction_id, session.session_id),
                    )
                    collision = await cursor.fetchone()
                    if collision is not None:
                        raise CorrelationError(
                            f"interaction {payload.interaction_id!r} belongs to "
                            f"session {collision['session_id']!r}"
                        )
                    if (
                        session.interaction_id is not None
                        and session.interaction_id != payload.interaction_id
                    ):
                        raise CorrelationError(
                            "webhook interaction does not match the linked interaction"
                        )

                if payload.status in {WebhookStatus.CONNECTED, WebhookStatus.COMPLETED}:
                    final_state = SessionState.COMPLETED
                elif payload.status is WebhookStatus.NO_ANSWER:
                    final_state = SessionState.NO_ANSWER
                elif payload.status is WebhookStatus.BUSY:
                    final_state = SessionState.BUSY
                else:
                    final_state = SessionState.FAILED

                if session.state in TERMINAL_SESSION_STATES and session.state is not final_state:
                    raise CorrelationError(
                        f"webhook status {payload.status.value!r} conflicts with "
                        f"terminal session state {session.state.value!r}"
                    )

                changes: dict[str, Any] = {
                    "attempt_id": payload.attempt_id,
                    "interaction_id": payload.interaction_id or session.interaction_id,
                    "state": final_state,
                    "ended_at": payload.occurred_at,
                }
                if payload.failure_reason is not None:
                    changes["failure_reason"] = payload.failure_reason
                updated = ContactSession.model_validate(
                    session.model_copy(update=changes).model_dump()
                )
                try:
                    await self._replace_session_locked(updated, timestamp=received)
                except sqlite3.IntegrityError:
                    raise CorrelationError(
                        "webhook provider identifiers are already linked elsewhere"
                    ) from None

                await self.connection.execute(
                    """
                    INSERT INTO provider_webhooks (
                        webhook_key, attempt_id, interaction_id, event_id,
                        session_id, status, payload_json, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        webhook_key,
                        payload.attempt_id,
                        payload.interaction_id,
                        session.event_id,
                        session.session_id,
                        payload.status.value,
                        payload_json,
                        _iso(received),
                    ),
                )

                if (
                    payload.interaction_id is not None
                    and session.interaction_id != payload.interaction_id
                ):
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=session.event_id,
                            session_id=session.session_id,
                            kind=TimelineKind.INTERACTION_LINKED,
                            details={"interaction_id": payload.interaction_id},
                            occurred_at=received,
                        )
                    )
                if session.state is not final_state:
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=session.event_id,
                            session_id=session.session_id,
                            kind=TimelineKind.SESSION_STATE_CHANGED,
                            from_state=session.state.value,
                            to_state=final_state.value,
                            details={"source": "completion_webhook"},
                            occurred_at=received,
                        )
                    )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=session.event_id,
                        session_id=session.session_id,
                        kind=TimelineKind.WEBHOOK_RECEIVED,
                        details={
                            "webhook_key": webhook_key,
                            "status": payload.status.value,
                        },
                        occurred_at=received,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return WebhookReceipt(
            webhook_key=webhook_key,
            event_id=session.event_id,
            session_id=session.session_id,
            created=True,
            status=payload.status,
        )

    register_webhook_once = record_webhook

    async def get_webhook(self, webhook_key: str) -> SarvamWebhookPayload | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM provider_webhooks WHERE webhook_key = ?",
                (webhook_key,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(SarvamWebhookPayload, row["payload_json"])

    async def create_fallback(self, fallback: FallbackLink) -> FallbackLink:
        """Persist one fallback capability per event without storing its bearer token."""

        if fallback.state is not FallbackState.PENDING:
            raise ValueError("new fallback links must start pending")
        payload = canonical_model_json(fallback)
        now = utc_now()
        async with self._write_lock:
            await self._begin()
            try:
                event = await self._fetch_event_locked(fallback.event_id)
                session = await self._fetch_session_locked(fallback.session_id)
                if event is None:
                    raise NotFoundError(f"event {fallback.event_id!r} does not exist")
                if session is None or session.event_id != fallback.event_id:
                    raise CorrelationError("fallback session does not belong to its event")
                cursor = await self.connection.execute(
                    "SELECT payload_json FROM fallback_links WHERE event_id = ?",
                    (fallback.event_id,),
                )
                existing_row = await cursor.fetchone()
                if existing_row is not None:
                    await self._commit()
                    return _load(FallbackLink, existing_row["payload_json"])
                await self.connection.execute(
                    """
                    INSERT INTO fallback_links (
                        fallback_id, event_id, session_id, state, expires_at,
                        payload_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        fallback.fallback_id,
                        fallback.event_id,
                        fallback.session_id,
                        fallback.state.value,
                        _iso(fallback.expires_at),
                        payload,
                        _iso(fallback.created_at),
                        _iso(now),
                    ),
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=fallback.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.FALLBACK_CREATED,
                        to_state=FallbackState.PENDING.value,
                        details={"reason": fallback.reason},
                        occurred_at=fallback.created_at,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return fallback

    async def get_fallback(self, fallback_id: str) -> FallbackLink | None:
        async with self._write_lock:
            return await self._fetch_fallback_locked(fallback_id)

    async def get_fallback_for_event(self, event_id: str) -> FallbackLink | None:
        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM fallback_links WHERE event_id = ?",
                (event_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(FallbackLink, row["payload_json"])

    async def activate_fallback(
        self,
        fallback_id: str,
        *,
        now: datetime | None = None,
    ) -> FallbackLink:
        """Mark delivery successful and keep the event waiting on the secure web channel."""

        timestamp = _as_utc(now)
        async with self._write_lock:
            await self._begin()
            try:
                fallback = await self._fetch_fallback_locked(fallback_id)
                if fallback is None:
                    raise NotFoundError(f"fallback {fallback_id!r} does not exist")
                if fallback.state in {FallbackState.DELIVERED, FallbackState.VERIFIED}:
                    await self._commit()
                    return fallback
                if fallback.state is not FallbackState.PENDING:
                    raise FallbackLinkError(f"cannot deliver {fallback.state.value} fallback")
                if fallback.expires_at <= timestamp:
                    raise FallbackLinkError("fallback expired before delivery")
                event = await self._fetch_event_locked(fallback.event_id)
                if event is None:
                    raise NotFoundError(f"event {fallback.event_id!r} does not exist")
                if event.state in TERMINAL_EVENT_STATES:
                    raise FallbackLinkError("fallback event is already terminal")
                delivered = fallback.model_copy(
                    update={
                        "state": FallbackState.DELIVERED,
                        "delivered_at": timestamp,
                    }
                )
                await self.connection.execute(
                    """
                    UPDATE fallback_links
                    SET state = ?, payload_json = ?, updated_at = ?
                    WHERE fallback_id = ?
                    """,
                    (
                        delivered.state.value,
                        canonical_model_json(delivered),
                        _iso(timestamp),
                        fallback_id,
                    ),
                )
                pending_event = event.model_copy(update={"state": EventState.FALLBACK_PENDING})
                await self.connection.execute(
                    """
                    UPDATE events
                    SET state = ?, payload_json = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (
                        pending_event.state.value,
                        canonical_model_json(pending_event),
                        _iso(timestamp),
                        event.event_id,
                    ),
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=event.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.FALLBACK_DELIVERED,
                        from_state=FallbackState.PENDING.value,
                        to_state=FallbackState.DELIVERED.value,
                        occurred_at=timestamp,
                    )
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=event.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.EVENT_STATE_CHANGED,
                        from_state=event.state.value,
                        to_state=EventState.FALLBACK_PENDING.value,
                        details={"reason": "secure_fallback_delivered"},
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return delivered

    async def fail_fallback_delivery(
        self,
        fallback_id: str,
        *,
        now: datetime | None = None,
    ) -> FallbackLink:
        """Close the event safely when its configured fallback channel cannot deliver."""

        timestamp = _as_utc(now)
        async with self._write_lock:
            await self._begin()
            try:
                fallback = await self._fetch_fallback_locked(fallback_id)
                if fallback is None:
                    raise NotFoundError(f"fallback {fallback_id!r} does not exist")
                if fallback.state is FallbackState.DELIVERY_FAILED:
                    await self._commit()
                    return fallback
                if fallback.state is not FallbackState.PENDING:
                    raise FallbackLinkError(
                        f"cannot fail delivery for {fallback.state.value} fallback"
                    )
                failed = fallback.model_copy(update={"state": FallbackState.DELIVERY_FAILED})
                await self.connection.execute(
                    """
                    UPDATE fallback_links
                    SET state = ?, payload_json = ?, updated_at = ?
                    WHERE fallback_id = ?
                    """,
                    (
                        failed.state.value,
                        canonical_model_json(failed),
                        _iso(timestamp),
                        fallback_id,
                    ),
                )
                event = await self._fetch_event_locked(fallback.event_id)
                if event is not None and event.state not in TERMINAL_EVENT_STATES:
                    failed_event = event.model_copy(update={"state": EventState.FAILED})
                    await self.connection.execute(
                        """
                        UPDATE events
                        SET state = ?, payload_json = ?, updated_at = ?
                        WHERE event_id = ?
                        """,
                        (
                            failed_event.state.value,
                            canonical_model_json(failed_event),
                            _iso(timestamp),
                            event.event_id,
                        ),
                    )
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=event.event_id,
                            session_id=fallback.session_id,
                            kind=TimelineKind.EVENT_STATE_CHANGED,
                            from_state=event.state.value,
                            to_state=EventState.FAILED.value,
                            details={"reason": "secure_fallback_delivery_failed"},
                            occurred_at=timestamp,
                        )
                    )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=fallback.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.FALLBACK_DELIVERY_FAILED,
                        from_state=fallback.state.value,
                        to_state=FallbackState.DELIVERY_FAILED.value,
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return failed

    async def verify_fallback(
        self,
        fallback_id: str,
        *,
        event_id: str,
        pin_matches: bool,
        max_attempts: int,
        now: datetime | None = None,
    ) -> FallbackLink:
        """Register a PIN attempt and issue a verified fallback state on success."""

        timestamp = _as_utc(now)
        failure: FallbackLinkError | None = None
        async with self._write_lock:
            await self._begin()
            try:
                fallback = await self._fetch_fallback_locked(fallback_id)
                if fallback is None or fallback.event_id != event_id:
                    failure = FallbackLinkError("fallback link is invalid")
                elif fallback.expires_at <= timestamp:
                    failure = FallbackLinkError("fallback link expired")
                elif fallback.state not in {FallbackState.DELIVERED, FallbackState.VERIFIED}:
                    failure = FallbackLinkError("fallback link is unavailable")
                elif not pin_matches:
                    attempts = fallback.verification_attempts + 1
                    revoked = attempts >= max_attempts
                    updated = fallback.model_copy(
                        update={
                            "verification_attempts": attempts,
                            "state": (FallbackState.REVOKED if revoked else fallback.state),
                        }
                    )
                    await self.connection.execute(
                        """
                        UPDATE fallback_links
                        SET state = ?, payload_json = ?, updated_at = ?
                        WHERE fallback_id = ?
                        """,
                        (
                            updated.state.value,
                            canonical_model_json(updated),
                            _iso(timestamp),
                            fallback_id,
                        ),
                    )
                    if revoked:
                        await self._fail_event_for_fallback_locked(
                            updated,
                            timestamp=timestamp,
                            reason="secure_fallback_verification_limit",
                        )
                        await self._insert_timeline_locked(
                            TimelineEntry(
                                event_id=updated.event_id,
                                session_id=updated.session_id,
                                kind=TimelineKind.FALLBACK_REVOKED,
                                from_state=fallback.state.value,
                                to_state=FallbackState.REVOKED.value,
                                occurred_at=timestamp,
                            )
                        )
                    fallback = updated
                    failure = FallbackLinkError("fallback verification failed")
                else:
                    if fallback.state is FallbackState.DELIVERED:
                        fallback = fallback.model_copy(
                            update={
                                "state": FallbackState.VERIFIED,
                                "verified_at": timestamp,
                            }
                        )
                        await self.connection.execute(
                            """
                            UPDATE fallback_links
                            SET state = ?, payload_json = ?, updated_at = ?
                            WHERE fallback_id = ?
                            """,
                            (
                                fallback.state.value,
                                canonical_model_json(fallback),
                                _iso(timestamp),
                                fallback_id,
                            ),
                        )
                        await self._insert_timeline_locked(
                            TimelineEntry(
                                event_id=fallback.event_id,
                                session_id=fallback.session_id,
                                kind=TimelineKind.FALLBACK_VERIFIED,
                                from_state=FallbackState.DELIVERED.value,
                                to_state=FallbackState.VERIFIED.value,
                                occurred_at=timestamp,
                            )
                        )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        if failure is not None:
            raise failure
        return fallback

    async def record_fallback_decision(
        self,
        fallback_id: str,
        decision: Decision,
        *,
        now: datetime | None = None,
    ) -> Decision:
        """Atomically consume a verified fallback and record its sole decision."""

        timestamp = _as_utc(now or decision.decided_at)
        payload = canonical_model_json(decision)
        async with self._write_lock:
            await self._begin()
            try:
                fallback = await self._fetch_fallback_locked(fallback_id)
                if fallback is None or fallback.event_id != decision.event_id:
                    raise FallbackLinkError("fallback link does not match the decision")
                if fallback.state is FallbackState.CONSUMED:
                    raise FallbackAlreadyConsumedError("fallback link was already consumed")
                if fallback.state is not FallbackState.VERIFIED:
                    raise FallbackLinkError("fallback link is not verified")
                if fallback.expires_at <= timestamp:
                    raise FallbackLinkError("fallback link expired")
                if decision.session_id != fallback.session_id:
                    raise CorrelationError("fallback decision session does not match")
                event = await self._fetch_event_locked(decision.event_id)
                if event is None:
                    raise NotFoundError(f"event {decision.event_id!r} does not exist")
                if event.state is not EventState.FALLBACK_PENDING:
                    raise FallbackLinkError("fallback event is not awaiting a response")
                await self._require_no_repository_context_locked(decision.event_id)
                cursor = await self.connection.execute(
                    "SELECT decision_id FROM decisions WHERE event_id = ?",
                    (decision.event_id,),
                )
                if await cursor.fetchone() is not None:
                    raise DecisionAlreadyExistsError(
                        f"event {decision.event_id!r} already has a decision"
                    )
                await self.connection.execute(
                    """
                    INSERT INTO decisions (
                        decision_id, event_id, session_id, payload_json, decided_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision.decision_id,
                        decision.event_id,
                        decision.session_id,
                        payload,
                        _iso(decision.decided_at),
                        _iso(timestamp),
                    ),
                )
                resolved = event.model_copy(update={"state": EventState.RESOLVED})
                await self.connection.execute(
                    """
                    UPDATE events
                    SET state = ?, payload_json = ?, updated_at = ?
                    WHERE event_id = ?
                    """,
                    (
                        resolved.state.value,
                        canonical_model_json(resolved),
                        _iso(timestamp),
                        event.event_id,
                    ),
                )
                consumed = fallback.model_copy(
                    update={
                        "state": FallbackState.CONSUMED,
                        "consumed_at": timestamp,
                    }
                )
                await self.connection.execute(
                    """
                    UPDATE fallback_links
                    SET state = ?, payload_json = ?, updated_at = ?
                    WHERE fallback_id = ?
                    """,
                    (
                        consumed.state.value,
                        canonical_model_json(consumed),
                        _iso(timestamp),
                        fallback_id,
                    ),
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=event.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.EVENT_STATE_CHANGED,
                        from_state=event.state.value,
                        to_state=EventState.RESOLVED.value,
                        details={"reason": "secure_fallback_decision"},
                        occurred_at=timestamp,
                    )
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=event.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.DECISION_RECORDED,
                        details={
                            "decision_id": decision.decision_id,
                            "outcome": decision.outcome.value,
                            "source": "secure_fallback",
                        },
                        occurred_at=decision.decided_at,
                    )
                )
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=event.event_id,
                        session_id=fallback.session_id,
                        kind=TimelineKind.FALLBACK_CONSUMED,
                        from_state=fallback.state.value,
                        to_state=FallbackState.CONSUMED.value,
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return decision

    async def expire_fallbacks(self, *, now: datetime | None = None) -> list[str]:
        """Expire stale links and fail only events still waiting on those links."""

        timestamp = _as_utc(now)
        event_ids: list[str] = []
        active_states = (
            FallbackState.PENDING.value,
            FallbackState.DELIVERED.value,
            FallbackState.VERIFIED.value,
        )
        placeholders = ",".join("?" for _ in active_states)
        async with self._write_lock:
            await self._begin()
            try:
                cursor = await self.connection.execute(
                    f"""
                    SELECT payload_json
                    FROM fallback_links
                    WHERE state IN ({placeholders}) AND expires_at <= ?
                    """,
                    (*active_states, _iso(timestamp)),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    fallback = _load(FallbackLink, row["payload_json"])
                    expired = fallback.model_copy(update={"state": FallbackState.EXPIRED})
                    await self.connection.execute(
                        """
                        UPDATE fallback_links
                        SET state = ?, payload_json = ?, updated_at = ?
                        WHERE fallback_id = ?
                        """,
                        (
                            expired.state.value,
                            canonical_model_json(expired),
                            _iso(timestamp),
                            expired.fallback_id,
                        ),
                    )
                    await self._fail_event_for_fallback_locked(
                        expired,
                        timestamp=timestamp,
                        reason="secure_fallback_expired",
                    )
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=expired.event_id,
                            session_id=expired.session_id,
                            kind=TimelineKind.FALLBACK_EXPIRED,
                            from_state=fallback.state.value,
                            to_state=FallbackState.EXPIRED.value,
                            occurred_at=timestamp,
                        )
                    )
                    event_ids.append(expired.event_id)
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return event_ids

    async def _fail_event_for_fallback_locked(
        self,
        fallback: FallbackLink,
        *,
        timestamp: datetime,
        reason: str,
    ) -> None:
        event = await self._fetch_event_locked(fallback.event_id)
        if event is None or event.state in TERMINAL_EVENT_STATES:
            return
        failed = event.model_copy(update={"state": EventState.FAILED})
        await self.connection.execute(
            """
            UPDATE events
            SET state = ?, payload_json = ?, updated_at = ?
            WHERE event_id = ?
            """,
            (
                failed.state.value,
                canonical_model_json(failed),
                _iso(timestamp),
                event.event_id,
            ),
        )
        await self._insert_timeline_locked(
            TimelineEntry(
                event_id=event.event_id,
                session_id=fallback.session_id,
                kind=TimelineKind.EVENT_STATE_CHANGED,
                from_state=event.state.value,
                to_state=EventState.FAILED.value,
                details={"reason": reason},
                occurred_at=timestamp,
            )
        )

    async def prepare_action(
        self,
        action: PreparedAction,
        *,
        dedupe_consumed: bool = False,
    ) -> PreparedAction:
        """Atomically register an action and optionally dedupe a demo execution."""

        if action.state is not ActionState.PREPARED:
            raise ValueError("new actions must start in the prepared state")
        if action.expires_at <= utc_now():
            raise ActionExpiredError("cannot prepare an already expired action")
        payload = canonical_model_json(action)
        async with self._write_lock:
            await self._begin()
            try:
                if await self._fetch_event_locked(action.event_id) is None:
                    raise NotFoundError(f"event {action.event_id!r} does not exist")
                await self._require_no_repository_context_locked(action.event_id)
                if action.session_id is not None:
                    session = await self._fetch_session_locked(action.session_id)
                    if session is None:
                        raise NotFoundError(f"session {action.session_id!r} does not exist")
                    if session.event_id != action.event_id:
                        raise CorrelationError(
                            "prepared action session does not belong to its event"
                        )
                if dedupe_consumed:
                    cursor = await self.connection.execute(
                        """
                        SELECT payload_json
                        FROM prepared_actions
                        WHERE event_id = ?
                          AND session_id IS ?
                          AND action_hash = ?
                          AND state IN (?, ?, ?)
                        ORDER BY CASE WHEN state = ? THEN 0 ELSE 1 END, updated_at DESC
                        LIMIT 1
                        """,
                        (
                            action.event_id,
                            action.session_id,
                            action.action_hash,
                            ActionState.PREPARED.value,
                            ActionState.CONFIRMED.value,
                            ActionState.CONSUMED.value,
                            ActionState.CONSUMED.value,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is not None:
                        await self._commit()
                        return _load(PreparedAction, row["payload_json"])
                try:
                    await self.connection.execute(
                        """
                        INSERT INTO prepared_actions (
                            action_id, event_id, session_id, action_hash,
                            confirmation_phrase_hash, state, expires_at,
                            payload_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            action.action_id,
                            action.event_id,
                            action.session_id,
                            action.action_hash,
                            action.confirmation_phrase_hash,
                            action.state.value,
                            _iso(action.expires_at),
                            payload,
                            _iso(action.created_at),
                            _iso(action.created_at),
                        ),
                    )
                except sqlite3.IntegrityError:
                    existing = await self._fetch_action_locked(action.action_id)
                    if existing is not None:
                        if not hmac.compare_digest(canonical_model_json(existing), payload):
                            raise ConflictError(
                                f"action_id {action.action_id!r} was reused with different content"
                            ) from None
                        await self._commit()
                        return existing
                    placeholders = ",".join("?" for _ in _ACTIVE_ACTION_VALUES)
                    cursor = await self.connection.execute(
                        f"""
                        SELECT payload_json
                        FROM prepared_actions
                        WHERE action_hash = ? AND state IN ({placeholders})
                        LIMIT 1
                        """,
                        (action.action_hash, *_ACTIVE_ACTION_VALUES),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        raise
                    await self._commit()
                    return _load(PreparedAction, row["payload_json"])
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=action.event_id,
                        session_id=action.session_id,
                        action_id=action.action_id,
                        kind=TimelineKind.ACTION_PREPARED,
                        to_state=ActionState.PREPARED.value,
                        details={
                            "action_hash": action.action_hash,
                            "risk": action.risk.value,
                        },
                        occurred_at=action.created_at,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return action

    async def get_action(self, action_id: str) -> PreparedAction | None:
        async with self._write_lock:
            return await self._fetch_action_locked(action_id)

    async def get_action_execution(self, action_id: str) -> TimelineEntry | None:
        """Return the latest durable demo execution outcome for one action."""

        async with self._write_lock:
            cursor = await self.connection.execute(
                """
                SELECT payload_json
                FROM timeline
                WHERE action_id = ? AND kind IN (?, ?)
                ORDER BY occurred_at DESC, timeline_id DESC
                LIMIT 1
                """,
                (
                    action_id,
                    TimelineKind.ACTION_EXECUTION_SUCCEEDED.value,
                    TimelineKind.ACTION_EXECUTION_FAILED.value,
                ),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(TimelineEntry, row["payload_json"])

    async def record_action_execution(
        self,
        action_id: str,
        *,
        succeeded: bool,
        message_to_user: str,
        operation_id: str | None = None,
        result: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> TimelineEntry:
        """Persist a redacted auto-execution receipt for idempotent retries."""

        timestamp = utc_now()
        async with self._write_lock:
            await self._begin()
            try:
                action = await self._fetch_action_locked(action_id)
                if action is None:
                    raise NotFoundError(f"action {action_id!r} does not exist")
                details: dict[str, Any] = {
                    "status": "succeeded" if succeeded else "failed",
                    "message_to_user": message_to_user,
                    "result": result or {},
                    "retryable": retryable,
                }
                if operation_id is not None:
                    details["operation_id"] = operation_id
                receipt = TimelineEntry(
                    event_id=action.event_id,
                    session_id=action.session_id,
                    action_id=action.action_id,
                    kind=(
                        TimelineKind.ACTION_EXECUTION_SUCCEEDED
                        if succeeded
                        else TimelineKind.ACTION_EXECUTION_FAILED
                    ),
                    details=details,
                    occurred_at=timestamp,
                )
                await self._insert_timeline_locked(receipt)
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return receipt

    async def _replace_action_locked(
        self,
        action: PreparedAction,
        *,
        timestamp: datetime,
    ) -> None:
        await self.connection.execute(
            """
            UPDATE prepared_actions
            SET state = ?, payload_json = ?, updated_at = ?
            WHERE action_id = ?
            """,
            (
                action.state.value,
                canonical_model_json(action),
                _iso(timestamp),
                action.action_id,
            ),
        )

    async def _replace_grant_locked(
        self,
        grant: ActionGrant,
        *,
        timestamp: datetime,
    ) -> None:
        await self.connection.execute(
            """
            UPDATE action_grants
            SET state = ?, consumed_at = ?, payload_json = ?, updated_at = ?
            WHERE grant_id = ?
            """,
            (
                grant.state.value,
                _iso(grant.consumed_at) if grant.consumed_at else None,
                canonical_model_json(grant),
                _iso(timestamp),
                grant.grant_id,
            ),
        )

    async def confirm_action(
        self,
        action_id: str,
        *,
        owner_ref: str,
        confirmation_method: ConfirmationMethod | str,
        confirmation_hash: str | None = None,
        now: datetime | None = None,
    ) -> ActionGrant:
        """Confirm an exact prepared action and mint its single durable grant."""

        confirmation_method = ConfirmationMethod(confirmation_method)
        timestamp = _as_utc(now)
        validate_owner_ref(owner_ref)
        expired = False
        async with self._write_lock:
            await self._begin()
            try:
                action = await self._fetch_action_locked(action_id)
                if action is None:
                    raise NotFoundError(f"action {action_id!r} does not exist")
                await self._require_no_repository_context_locked(action.event_id)
                if action.state in {ActionState.EXPIRED, ActionState.CANCELLED}:
                    raise ActionExpiredError(
                        f"action is no longer confirmable ({action.state.value})"
                    )
                if action.state is ActionState.CONSUMED:
                    raise ActionAlreadyConsumedError("action grant has already been consumed")
                if timestamp >= action.expires_at:
                    expired_action = action.model_copy(update={"state": ActionState.EXPIRED})
                    await self._replace_action_locked(expired_action, timestamp=timestamp)
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=action.event_id,
                            session_id=action.session_id,
                            action_id=action.action_id,
                            kind=TimelineKind.ACTION_EXPIRED,
                            from_state=action.state.value,
                            to_state=ActionState.EXPIRED.value,
                            occurred_at=timestamp,
                        )
                    )
                    expired = True
                else:
                    if action.requires_confirmation:
                        if confirmation_method is ConfirmationMethod.NOT_REQUIRED:
                            raise ActionHashMismatchError(
                                "this action requires an explicit confirmation method"
                            )
                        if confirmation_hash is None or not hmac.compare_digest(
                            confirmation_hash,
                            action.confirmation_phrase_hash or "",
                        ):
                            raise ActionHashMismatchError(
                                "confirmation does not match the prepared action"
                            )
                    elif confirmation_method is not ConfirmationMethod.NOT_REQUIRED:
                        raise ConflictError("unconfirmed low-risk actions must use not_required")

                    cursor = await self.connection.execute(
                        "SELECT payload_json FROM action_grants WHERE action_id = ?",
                        (action.action_id,),
                    )
                    row = await cursor.fetchone()
                    if row is not None:
                        grant = _load(ActionGrant, row["payload_json"])
                        if grant.owner_ref != owner_ref:
                            raise ConflictError(
                                "action was already confirmed by a different owner reference"
                            )
                    else:
                        grant = ActionGrant(
                            action_id=action.action_id,
                            event_id=action.event_id,
                            session_id=action.session_id,
                            action_hash=action.action_hash,
                            owner_ref=owner_ref,
                            confirmation_method=confirmation_method,
                            confirmed_at=timestamp,
                            expires_at=action.expires_at,
                        )
                        await self.connection.execute(
                            """
                            INSERT INTO action_grants (
                                grant_id, action_id, event_id, session_id, action_hash,
                                owner_ref, state, confirmed_at, expires_at, consumed_at,
                                payload_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                grant.grant_id,
                                grant.action_id,
                                grant.event_id,
                                grant.session_id,
                                grant.action_hash,
                                grant.owner_ref,
                                grant.state.value,
                                _iso(grant.confirmed_at),
                                _iso(grant.expires_at),
                                None,
                                canonical_model_json(grant),
                                _iso(timestamp),
                            ),
                        )
                    if action.state is ActionState.PREPARED:
                        confirmed_action = action.model_copy(
                            update={"state": ActionState.CONFIRMED}
                        )
                        await self._replace_action_locked(
                            confirmed_action,
                            timestamp=timestamp,
                        )
                        await self._insert_timeline_locked(
                            TimelineEntry(
                                event_id=action.event_id,
                                session_id=action.session_id,
                                action_id=action.action_id,
                                kind=TimelineKind.ACTION_CONFIRMED,
                                from_state=ActionState.PREPARED.value,
                                to_state=ActionState.CONFIRMED.value,
                                details={
                                    "grant_id": grant.grant_id,
                                    "confirmation_method": confirmation_method.value,
                                },
                                occurred_at=timestamp,
                            )
                        )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        if expired:
            raise ActionExpiredError("action expired before confirmation")
        return grant

    async def get_grant(self, grant_id: str) -> ActionGrant | None:
        async with self._write_lock:
            return await self._fetch_grant_locked(grant_id)

    async def get_grant_for_action(self, action_id: str) -> ActionGrant | None:
        """Return the one durable grant bound to a prepared action."""

        async with self._write_lock:
            cursor = await self.connection.execute(
                "SELECT payload_json FROM action_grants WHERE action_id = ?",
                (action_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _load(ActionGrant, row["payload_json"])

    async def consume_action(
        self,
        grant_id: str,
        *,
        action_hash: str,
        now: datetime | None = None,
    ) -> ActionGrant:
        """Atomically consume a hash-bound grant exactly once."""

        timestamp = _as_utc(now)
        expired = False
        async with self._write_lock:
            await self._begin()
            try:
                grant = await self._fetch_grant_locked(grant_id)
                if grant is None:
                    raise NotFoundError(f"grant {grant_id!r} does not exist")
                if not hmac.compare_digest(grant.action_hash, action_hash):
                    raise ActionHashMismatchError("grant does not authorize this action hash")
                action = await self._fetch_action_locked(grant.action_id)
                if action is None:
                    raise CorrelationError("grant references a missing prepared action")
                await self._require_no_repository_context_locked(action.event_id)
                if not hmac.compare_digest(action.action_hash, action_hash):
                    raise ActionHashMismatchError(
                        "prepared action no longer matches the grant hash"
                    )
                if grant.state is GrantState.CONSUMED or action.state is ActionState.CONSUMED:
                    raise ActionAlreadyConsumedError("action grant has already been consumed")
                if grant.state in {GrantState.EXPIRED, GrantState.REVOKED}:
                    raise ActionExpiredError(f"grant is no longer usable ({grant.state.value})")
                if timestamp >= min(grant.expires_at, action.expires_at):
                    expired_grant = grant.model_copy(update={"state": GrantState.EXPIRED})
                    expired_action = action.model_copy(update={"state": ActionState.EXPIRED})
                    await self._replace_grant_locked(expired_grant, timestamp=timestamp)
                    await self._replace_action_locked(expired_action, timestamp=timestamp)
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=action.event_id,
                            session_id=action.session_id,
                            action_id=action.action_id,
                            kind=TimelineKind.ACTION_EXPIRED,
                            from_state=action.state.value,
                            to_state=ActionState.EXPIRED.value,
                            details={"grant_id": grant.grant_id},
                            occurred_at=timestamp,
                        )
                    )
                    expired = True
                else:
                    cursor = await self.connection.execute(
                        """
                        UPDATE action_grants
                        SET state = ?
                        WHERE grant_id = ? AND state = ?
                        """,
                        (
                            GrantState.CONSUMED.value,
                            grant.grant_id,
                            GrantState.CONFIRMED.value,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise ActionAlreadyConsumedError("action grant was consumed concurrently")
                    consumed_grant = grant.model_copy(
                        update={
                            "state": GrantState.CONSUMED,
                            "consumed_at": timestamp,
                        }
                    )
                    consumed_action = action.model_copy(update={"state": ActionState.CONSUMED})
                    await self._replace_grant_locked(
                        consumed_grant,
                        timestamp=timestamp,
                    )
                    await self._replace_action_locked(
                        consumed_action,
                        timestamp=timestamp,
                    )
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=action.event_id,
                            session_id=action.session_id,
                            action_id=action.action_id,
                            kind=TimelineKind.ACTION_CONSUMED,
                            from_state=ActionState.CONFIRMED.value,
                            to_state=ActionState.CONSUMED.value,
                            details={"grant_id": grant.grant_id},
                            occurred_at=timestamp,
                        )
                    )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        if expired:
            raise ActionExpiredError("action grant expired before consumption")
        return consumed_grant

    async def cancel_action(
        self,
        action_id: str,
        *,
        occurred_at: datetime | None = None,
    ) -> PreparedAction:
        timestamp = _as_utc(occurred_at)
        async with self._write_lock:
            await self._begin()
            try:
                action = await self._fetch_action_locked(action_id)
                if action is None:
                    raise NotFoundError(f"action {action_id!r} does not exist")
                if action.state is ActionState.CANCELLED:
                    await self._commit()
                    return action
                if action.state in {ActionState.CONSUMED, ActionState.EXPIRED}:
                    raise InvalidStateTransitionError(f"cannot cancel {action.state.value} action")
                cancelled = action.model_copy(update={"state": ActionState.CANCELLED})
                await self._replace_action_locked(cancelled, timestamp=timestamp)
                cursor = await self.connection.execute(
                    "SELECT payload_json FROM action_grants WHERE action_id = ?",
                    (action_id,),
                )
                row = await cursor.fetchone()
                if row is not None:
                    grant = _load(ActionGrant, row["payload_json"])
                    if grant.state is GrantState.CONFIRMED:
                        revoked = grant.model_copy(update={"state": GrantState.REVOKED})
                        await self._replace_grant_locked(revoked, timestamp=timestamp)
                await self._insert_timeline_locked(
                    TimelineEntry(
                        event_id=action.event_id,
                        session_id=action.session_id,
                        action_id=action.action_id,
                        kind=TimelineKind.ACTION_CANCELLED,
                        from_state=action.state.value,
                        to_state=ActionState.CANCELLED.value,
                        occurred_at=timestamp,
                    )
                )
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return cancelled

    async def expire_actions(self, *, now: datetime | None = None) -> int:
        """Persist expiry for all due, unconsumed actions and grants."""

        timestamp = _as_utc(now)
        expired_count = 0
        async with self._write_lock:
            await self._begin()
            try:
                placeholders = ",".join("?" for _ in _ACTIVE_ACTION_VALUES)
                cursor = await self.connection.execute(
                    f"""
                    SELECT payload_json
                    FROM prepared_actions
                    WHERE state IN ({placeholders}) AND expires_at <= ?
                    """,
                    (*_ACTIVE_ACTION_VALUES, _iso(timestamp)),
                )
                rows = await cursor.fetchall()
                for row in rows:
                    action = _load(PreparedAction, row["payload_json"])
                    expired_action = action.model_copy(update={"state": ActionState.EXPIRED})
                    await self._replace_action_locked(
                        expired_action,
                        timestamp=timestamp,
                    )
                    grant_cursor = await self.connection.execute(
                        "SELECT payload_json FROM action_grants WHERE action_id = ?",
                        (action.action_id,),
                    )
                    grant_row = await grant_cursor.fetchone()
                    if grant_row is not None:
                        grant = _load(ActionGrant, grant_row["payload_json"])
                        if grant.state is GrantState.CONFIRMED:
                            expired_grant = grant.model_copy(update={"state": GrantState.EXPIRED})
                            await self._replace_grant_locked(
                                expired_grant,
                                timestamp=timestamp,
                            )
                    await self._insert_timeline_locked(
                        TimelineEntry(
                            event_id=action.event_id,
                            session_id=action.session_id,
                            action_id=action.action_id,
                            kind=TimelineKind.ACTION_EXPIRED,
                            from_state=action.state.value,
                            to_state=ActionState.EXPIRED.value,
                            occurred_at=timestamp,
                        )
                    )
                    expired_count += 1
            except BaseException:
                await self._rollback()
                raise
            await self._commit()
        return expired_count

    async def list_timeline(
        self,
        *,
        event_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[TimelineEntry]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        clauses: list[str] = []
        parameters: list[Any] = []
        if event_id is not None:
            clauses.append("event_id = ?")
            parameters.append(event_id)
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        query = "SELECT payload_json FROM timeline"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY occurred_at ASC, timeline_id ASC LIMIT ?"
        parameters.append(limit)
        async with self._write_lock:
            cursor = await self.connection.execute(query, parameters)
            rows = await cursor.fetchall()
        return [_load(TimelineEntry, row["payload_json"]) for row in rows]


# Short aliases make dependency injection terse while preserving a descriptive
# concrete class name for type checking and documentation.
Store = SQLiteStore
Storage = SQLiteStore


__all__ = [
    "ActionAlreadyConsumedError",
    "ActionExpiredError",
    "ActionHashMismatchError",
    "ActiveSessionError",
    "ConflictError",
    "CorrelationError",
    "DecisionAlreadyExistsError",
    "FallbackAlreadyConsumedError",
    "FallbackLinkError",
    "InvalidStateTransitionError",
    "NotFoundError",
    "SQLiteStore",
    "Storage",
    "StorageError",
    "Store",
    "StoreNotInitializedError",
]
