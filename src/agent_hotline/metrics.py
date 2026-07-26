"""Timeline metric derivation for the demo and audit API."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TimelineMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_detected_at: datetime | None = None
    call_started_at: datetime | None = None
    call_answered_at: datetime | None = None
    decision_recorded_at: datetime | None = None
    agent_resumed_at: datetime | None = None
    action_completed_at: datetime | None = None
    time_to_contact_seconds: float | None = None
    time_to_decision_seconds: float | None = None
    time_to_resume_seconds: float | None = None
    time_to_completion_seconds: float | None = None


_EVENT_TO_FIELD = {
    "event_detected": "event_detected_at",
    "call_started": "call_started_at",
    "call_answered": "call_answered_at",
    "decision_recorded": "decision_recorded_at",
    "agent_resumed": "agent_resumed_at",
    "action_completed": "action_completed_at",
}


def derive_timeline_metrics(
    entries: Iterable[Mapping[str, object]],
) -> TimelineMetrics:
    timestamps: dict[str, datetime] = {}
    for entry in entries:
        event_type = entry.get("event_type") or entry.get("kind")
        raw_timestamp = entry.get("created_at") or entry.get("timestamp")
        if not isinstance(event_type, str):
            continue
        field = _EVENT_TO_FIELD.get(event_type)
        if not field:
            continue
        timestamp = _coerce_datetime(raw_timestamp)
        if timestamp is not None and field not in timestamps:
            timestamps[field] = timestamp

    detected = timestamps.get("event_detected_at")
    started = timestamps.get("call_started_at")
    answered = timestamps.get("call_answered_at")
    decided = timestamps.get("decision_recorded_at")
    resumed = timestamps.get("agent_resumed_at")
    completed = timestamps.get("action_completed_at")

    return TimelineMetrics(
        **timestamps,
        time_to_contact_seconds=_duration(detected, answered or started),
        time_to_decision_seconds=_duration(detected, decided),
        time_to_resume_seconds=_duration(detected, resumed),
        time_to_completion_seconds=_duration(detected, completed),
    )


def display_state(event_state: str, contact_state: str | None = None) -> str:
    """Derive the compact demo label without conflating domain states."""

    if event_state in {"completed", "action_completed"}:
        return "DEPLOYED"
    if event_state in {"resumed", "agent_resumed"}:
        return "RESUMED"
    if event_state in {"resolved", "approved"}:
        return "APPROVED"
    if contact_state in {"connected", "discussing", "awaiting_decision"}:
        return "DISCUSSING"
    if contact_state in {"queued", "dialing", "calling"}:
        return "CALLING"
    return "BLOCKED"


def _duration(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max((end - start).total_seconds(), 0.0)


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
