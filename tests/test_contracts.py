from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_hotline.contracts import (
    ConfirmActionRequest,
    ContactHumanRequest,
    RecordInstructionRequest,
)


def test_contact_request_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        ContactHumanRequest(
            kind="incident",
            summary="Database capacity is exhausted.",
            question="Raise capacity or pause traffic?",
            deadline=datetime.now(),
        )


def test_contact_request_accepts_scoped_context() -> None:
    request = ContactHumanRequest(
        source="claude_mcp",
        kind="compute_interrupted",
        severity="high",
        summary="The spot instance ended at epoch 17.",
        question="Resume on spot, switch to on-demand, or stop?",
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        dedupe_key="training-run-epoch-17",
    )
    assert request.source == "claude_mcp"
    assert request.deadline is not None


@pytest.mark.parametrize(
    "outcome",
    ["approve", "deny", "instruct", "defer", "auth_completed"],
)
def test_every_authoritative_decision_requires_confirmation_pin(outcome: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        RecordInstructionRequest(
            event_id="evt_1",
            outcome=outcome,
            instruction="Deploy.",
        )
    assert exc_info.value.errors()[0]["loc"] == ("confirmation_pin",)


def test_empty_pin_is_rejected_for_every_decision() -> None:
    with pytest.raises(ValidationError, match="confirmation PIN"):
        RecordInstructionRequest(
            event_id="evt_1",
            outcome="instruct",
            instruction="Pause and wait.",
            confirmation_pin="",
        )


def test_confirmation_requires_secret_typed_pin_and_excludes_it_from_dumps() -> None:
    confirmation = ConfirmActionRequest(
        event_id="evt_1",
        action_id="act_1",
        confirmation_nonce="nonce-123456789",
        exact_confirmation="confirm production shutdown",
        confirmation_method="spoken_plus_dtmf",
        confirmation_pin="246810",
    )
    assert confirmation.confirmation_pin.get_secret_value() == "246810"
    assert "confirmation_pin" not in confirmation.model_dump(mode="json")
    assert "246810" not in repr(confirmation)
    decision = RecordInstructionRequest(
        event_id="evt_1",
        outcome="instruct",
        instruction="Pause and wait.",
        confirmation_pin="246810",
    )
    assert decision.confirmation_pin.get_secret_value() == "246810"
    assert "confirmation_pin" not in decision.model_dump(mode="json")
    assert "246810" not in repr(decision)


@pytest.mark.parametrize(
    "pin",
    ["12345", "1234567890123", "abcdef", "\uff11\uff12\uff13\uff14\uff15\uff16"],
)
def test_confirmation_pin_has_bounded_ascii_dtmf_shape(pin: str) -> None:
    with pytest.raises(ValidationError, match="ASCII digits"):
        ConfirmActionRequest(
            event_id="evt_1",
            action_id="act_1",
            confirmation_nonce="nonce-123456789",
            exact_confirmation="confirm production shutdown",
            confirmation_method="spoken_plus_dtmf",
            confirmation_pin=pin,
        )
