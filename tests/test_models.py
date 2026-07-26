from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_hotline.models import (
    ActionKind,
    ActionScope,
    ContactDirection,
    ContactSession,
    Decision,
    EscalationEvent,
    PreparedAction,
    RiskLevel,
    SarvamWebhookPayload,
    Severity,
    utc_now,
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_models_reject_unknown_enums_and_fields() -> None:
    with pytest.raises(ValidationError):
        EscalationEvent(
            kind="make_it_so",
            summary="Unknown kind",
            severity=Severity.WARNING,
        )

    with pytest.raises(ValidationError):
        EscalationEvent(
            kind="incident",
            summary="Database is unavailable",
            invented=True,
        )


def test_decision_bearing_event_requires_a_question_and_aware_deadline() -> None:
    with pytest.raises(ValidationError, match="question is required"):
        EscalationEvent(kind="approval", summary="Ready to deploy")

    with pytest.raises(ValidationError, match="timezone"):
        EscalationEvent(
            kind="approval",
            summary="Ready to deploy",
            question="May I deploy?",
            deadline_at=datetime(2026, 7, 26, 12, 0),
        )


def test_raw_phone_and_secret_material_are_rejected() -> None:
    with pytest.raises(ValidationError, match="pattern"):
        ContactSession(
            direction=ContactDirection.OUTBOUND_ESCALATION,
            owner_ref="+1 202 555 0123",
        )

    with pytest.raises(ValidationError, match="sensitive field"):
        EscalationEvent(
            kind="incident",
            summary="Provider failed",
            evidence={"api_key": "do-not-store-this"},
        )

    with pytest.raises(ValidationError, match="raw phone"):
        SarvamWebhookPayload(
            attempt_id="attempt_123",
            status="failed",
            metadata={"callback": "+12025550123"},
        )


@pytest.mark.parametrize(
    ("outcome", "instruction"),
    [
        ("approve", None),
        ("deny", None),
        ("instruct", "Pause and wait."),
        ("defer", None),
        ("auth_completed", None),
    ],
)
def test_every_resolved_decision_requires_verified_identity(
    outcome: str,
    instruction: str | None,
) -> None:
    event = EscalationEvent(
        kind="approval",
        summary="Tests pass",
        question="Approve deploy?",
    )
    with pytest.raises(ValidationError, match="verified identity"):
        Decision(
            event_id=event.event_id,
            outcome=outcome,
            instruction=instruction,
        )

    decision = Decision(
        event_id=event.event_id,
        outcome=outcome,
        instruction=instruction,
        identity_verified=True,
    )
    assert decision.identity_verified is True


def test_high_risk_prepared_action_is_expiring_and_hash_bound() -> None:
    event = EscalationEvent(kind="incident", summary="Production is failing")
    now = utc_now()

    with pytest.raises(ValidationError, match="require confirmation"):
        PreparedAction(
            event_id=event.event_id,
            kind=ActionKind.DEPLOYMENT,
            target="api-service/production",
            scope=ActionScope(host_id="local", environment="production"),
            risk=RiskLevel.HIGH,
            action_hash=digest("deploy"),
            requires_confirmation=False,
            expires_at=now + timedelta(minutes=5),
        )

    action = PreparedAction(
        event_id=event.event_id,
        kind=ActionKind.DEPLOYMENT,
        target="api-service/production",
        scope=ActionScope(host_id="local", environment="production"),
        risk=RiskLevel.HIGH,
        action_hash=digest("deploy"),
        confirmation_phrase_hash=digest("confirm production deploy"),
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    assert action.expires_at.tzinfo is UTC
