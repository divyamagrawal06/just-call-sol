"""Deterministic fallback demo and the live-call demonstration entry point."""

from __future__ import annotations

import secrets
from typing import Any

from pydantic import SecretStr

from .client import HotlineClient
from .contracts import (
    ConfirmActionRequest,
    ContactHumanRequest,
    ContextPacket,
    ExecuteActionRequest,
    PrepareActionRequest,
    ProposedAction,
    RecordInstructionRequest,
)
from .coordinator import HotlineCoordinator
from .providers import FakeCallProvider
from .runbooks import create_default_registry
from .settings import get_settings
from .storage import SQLiteStore


def demo_request(*, wait_for_decision: bool) -> ContactHumanRequest:
    return ContactHumanRequest(
        source="demo",
        kind="incident",
        severity="critical",
        summary="The demo orders database is out of request units and requests are failing.",
        question="Increase the mock database limit, keep the system paused, or defer?",
        proposed_actions=[
            ProposedAction(
                action_type="demo.increase_db_ru_limit",
                summary="Increase demo-orders from 400 to 800 RU.",
                parameters={
                    "environment": "demo",
                    "database": "demo-orders",
                    "current_ru": 400,
                    "target_ru": 800,
                },
                risk="high",
            )
        ],
        context=ContextPacket(
            task_summary="Restore the deterministic demo orders service.",
            agent_summary="Three requests failed after the mock RU budget was exhausted.",
            last_error="HTTP 429: request-unit budget exhausted.",
            pending_action_summary="Increase demo-orders from 400 to 800 RU.",
            owner_constraints=["No real cloud or database resource may be changed."],
        ),
        no_answer_policy="pause",
        dedupe_key="demo-db-ru-exhausted-v1",
        wait_for_decision=wait_for_decision,
        timeout_seconds=600 if wait_for_decision else 1,
    )


async def run_demo(*, auto_decide: bool = False) -> dict[str, Any]:
    """Run a real managed call, or prove the complete grant flow with only mocks."""

    if not auto_decide:
        async with HotlineClient() as client:
            result = await client.contact_human(demo_request(wait_for_decision=True))
        return {
            "mode": "configured_provider",
            "result": result.model_dump(mode="json", exclude_none=True),
        }

    demo_confirmation_pin = SecretStr(f"{secrets.randbelow(100_000_000):08d}")
    settings = get_settings().model_copy(
        update={
            "hotline_database_path": get_settings().hotline_database_path.with_name(
                "demo-hotline.db"
            ),
            "hotline_transport": "fake",
            "hotline_allow_real_actions": False,
            "owner_confirmation_pin": demo_confirmation_pin,
        }
    )
    settings.ensure_runtime_directory()
    store = SQLiteStore(settings.hotline_database_path)
    provider = FakeCallProvider()
    await store.initialize()
    coordinator = HotlineCoordinator(
        settings=settings,
        store=store,
        provider=provider,
        runbooks=create_default_registry(allow_real_execution=False),
    )
    try:
        call = await coordinator.contact_human(demo_request(wait_for_decision=False))
        prepared = await coordinator.prepare_action(
            PrepareActionRequest(
                event_id=call.event_id,
                action_type="demo.increase_db_ru_limit",
                parameters={
                    "environment": "demo",
                    "database": "demo-orders",
                    "current_ru": 400,
                    "target_ru": 800,
                },
            )
        )
        phrase = prepared.exact_readback.rsplit("say exactly: ", maxsplit=1)[-1]
        confirmed = await coordinator.confirm_action(
            ConfirmActionRequest(
                event_id=call.event_id,
                action_id=prepared.action_id,
                confirmation_nonce=prepared.confirmation_nonce,
                exact_confirmation=phrase,
                confirmation_method="spoken_plus_dtmf",
                confirmation_pin=demo_confirmation_pin,
            )
        )
        if not confirmed.confirmed or confirmed.grant_id is None:
            raise RuntimeError("deterministic demo confirmation unexpectedly failed")
        decision = await coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=call.event_id,
                outcome="approve",
                instruction="Run only the exact confirmed mock RU increase.",
                approved_action_ids=[prepared.action_id],
                confirmation_method="spoken_plus_dtmf",
                confirmation_pin=demo_confirmation_pin,
            )
        )
        execution = await coordinator.execute_action(
            ExecuteActionRequest(
                event_id=call.event_id,
                action_id=prepared.action_id,
                grant_id=confirmed.grant_id,
            )
        )
        return {
            "mode": "deterministic_mock",
            "identity_mode": "simulated-for-mock-only",
            "real_resources_changed": False,
            "event_id": call.event_id,
            "attempt_id": call.attempt_id,
            "decision_id": decision.decision_id,
            "action_id": execution.action_id,
            "operation_id": execution.operation_id,
            "status": execution.result.get("status"),
            "verified": execution.result.get("verified"),
            "message": execution.message_to_user,
        }
    finally:
        await provider.close()
        await store.close()
