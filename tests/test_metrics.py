from datetime import UTC, datetime, timedelta

from agent_hotline.metrics import derive_timeline_metrics, display_state


def test_derive_metrics_uses_first_timestamp_per_stage() -> None:
    start = datetime.now(UTC)
    metrics = derive_timeline_metrics(
        [
            {"event_type": "event_detected", "created_at": start},
            {"event_type": "call_started", "created_at": start + timedelta(seconds=2)},
            {"event_type": "call_answered", "created_at": start + timedelta(seconds=5)},
            {"event_type": "decision_recorded", "created_at": start + timedelta(seconds=35)},
            {"event_type": "agent_resumed", "created_at": start + timedelta(seconds=37)},
            {"event_type": "action_completed", "created_at": start + timedelta(seconds=50)},
        ]
    )
    assert metrics.time_to_contact_seconds == 5
    assert metrics.time_to_decision_seconds == 35
    assert metrics.time_to_resume_seconds == 37
    assert metrics.time_to_completion_seconds == 50


def test_display_state_is_derived_not_persisted() -> None:
    assert display_state("pending", "dialing") == "CALLING"
    assert display_state("pending", "connected") == "DISCUSSING"
    assert display_state("resolved", "connected") == "APPROVED"
    assert display_state("resumed") == "RESUMED"
    assert display_state("completed") == "DEPLOYED"
