"""Durable orchestration between MCP callers, Sarvam, and agent controls."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import JsonValue, SecretStr

from .codex_app_server import SafeThreadController
from .contracts import (
    BeginInboundSessionRequest,
    BeginInboundSessionResponse,
    ConfirmActionRequest,
    ConfirmActionResponse,
    ContactHumanRequest,
    ContactHumanResult,
    ContextPacket,
    EscalationContextRequest,
    EscalationContextResponse,
    EventSummary,
    ExecuteActionRequest,
    ExecuteActionResponse,
    FallbackDecisionRequest,
    FallbackDecisionResponse,
    FallbackOpenRequest,
    FallbackOpenResponse,
    PrepareActionRequest,
    PrepareActionResponse,
    RecordInstructionRequest,
    RecordInstructionResponse,
    RepositoryContextQuery,
    RepositoryContextResponse,
    SarvamRepositoryContextRequest,
    ThreadInspectRequest,
    ThreadListRequest,
)
from .fallback_delivery import (
    FallbackNotification,
    FallbackNotifier,
)
from .models import (
    ActionKind,
    ActionScope,
    AgentType,
    ConfirmationMethod,
    ContactChannel,
    ContactDirection,
    ContactSession,
    ContextReference,
    ContextSnapshot,
    Decision,
    DecisionOutcome,
    DecisionSource,
    EscalationEvent,
    EscalationKind,
    EventSource,
    EventState,
    FallbackLink,
    NoAnswerPolicy,
    PendingActionSnapshot,
    PreparedAction,
    ProposedAction,
    RiskLevel,
    SarvamWebhookPayload,
    SessionState,
    Severity,
    TimelineEntry,
    TimelineKind,
    TranscriptRole,
    TranscriptTurn,
    WebhookStatus,
    utc_now,
)
from .providers import CallProvider
from .repository_context import RepositoryContextService
from .runbooks import RunbookRegistry
from .sarvam import InstantOutboundWebhook
from .security import (
    CallerAllowlist,
    ExpiredTokenError,
    ExpiringTokenSigner,
    InvalidTokenError,
    action_hash,
    redact_secrets,
    sanitize_untrusted_text,
)
from .settings import Settings
from .storage import (
    ActiveSessionError,
    DecisionAlreadyExistsError,
    FallbackLinkError,
    InvalidStateTransitionError,
    NotFoundError,
    SQLiteStore,
)

logger = logging.getLogger(__name__)

_CONTACT_KIND_MAP: dict[str, EscalationKind] = {
    "approval": EscalationKind.APPROVAL,
    "clarification": EscalationKind.AMBIGUITY,
    "incident": EscalationKind.INCIDENT,
    "compute_interrupted": EscalationKind.INCIDENT,
    "authentication": EscalationKind.AUTHENTICATION,
    "completion": EscalationKind.COMPLETION,
    "provider_failure": EscalationKind.INCIDENT,
    "other": EscalationKind.STATUS,
}
_SOURCE_MAP: dict[str, EventSource] = {item.value: item for item in EventSource}
_NO_ANSWER_MAP: dict[str, NoAnswerPolicy] = {
    "pause": NoAnswerPolicy.PAUSE,
    "defer": NoAnswerPolicy.TEXT_AND_PAUSE,
    "notify_only": NoAnswerPolicy.CONTINUE_SAFELY,
}
_CONFIRMATION_MAP: dict[str, ConfirmationMethod] = {
    "spoken_phrase": ConfirmationMethod.SPOKEN_PHRASE,
    "dtmf": ConfirmationMethod.DTMF_PIN,
    "spoken_plus_dtmf": ConfirmationMethod.DTMF_PIN,
    "out_of_band": ConfirmationMethod.SIGNED_TOKEN,
}
_THREAD_ACTIONS: dict[str, tuple[ActionKind, RiskLevel]] = {
    "thread.instruct": (ActionKind.THREAD_INSTRUCTION, RiskLevel.MEDIUM),
    "thread.interrupt": (ActionKind.INTERRUPT_THREAD, RiskLevel.MEDIUM),
    "thread.spawn_root": (ActionKind.SPAWN_ROOT_THREAD, RiskLevel.MEDIUM),
    "thread.archive": (ActionKind.DEPLOYMENT, RiskLevel.HIGH),
}
_THREAD_STATUS_QUERY_ALIASES: dict[str, frozenset[str]] = {
    "running": frozenset({"active"}),
    "in progress": frozenset({"active"}),
}
_VOICE_THREAD_STATUS_QUERY_LIMIT = 10
_VOICE_THREAD_QUERY_SCAN_LIMIT = 100


class HotlineCoordinator:
    """Own the state machine and keep provider/model output non-authoritative."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        provider: CallProvider,
        runbooks: RunbookRegistry,
        controller: SafeThreadController | None = None,
        repository_context: RepositoryContextService | None = None,
        fallback_notifier: FallbackNotifier | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self.runbooks = runbooks
        self.controller = controller
        self.fallback_notifier = fallback_notifier
        self.repository_context = repository_context or RepositoryContextService(
            settings,
            known_secrets=(
                settings.sarvam_api_key.get_secret_value(),
                settings.hotline_tool_token.get_secret_value(),
                settings.hotline_local_token.get_secret_value(),
                settings.hotline_callback_token.get_secret_value(),
                settings.owner_confirmation_pin.get_secret_value(),
                settings.hotline_fallback_webhook_token.get_secret_value(),
            ),
        )
        signing_secret = (
            settings.hotline_callback_token.get_secret_value()
            or settings.hotline_tool_token.get_secret_value()
            or settings.hotline_fallback_webhook_token.get_secret_value()
        )
        if len(signing_secret) < 16:
            # This branch is useful for isolated unit tests. Production startup
            # rejects empty public tokens before any tool route can be reached.
            signing_secret = "development-only-hotline-signing-key"
        self._signer = ExpiringTokenSigner(signing_secret, max_ttl_seconds=300)
        self._fallback_signer = ExpiringTokenSigner(
            signing_secret,
            issuer="agent-hotline-fallback",
            max_ttl_seconds=settings.hotline_fallback_ttl_seconds,
        )
        self._waiters: dict[str, asyncio.Event] = {}

    async def contact_human(self, request: ContactHumanRequest) -> ContactHumanResult:
        event = self._event_from_request(request)
        ingested = await self.store.create_event(event)
        event = ingested.event

        if not ingested.created:
            if request.wait_for_decision:
                resolved = await self._wait_for_result(
                    event.event_id,
                    timeout_seconds=request.timeout_seconds,
                )
                if resolved is not None:
                    return resolved
            return await self._result_for_event(event.event_id, duplicate=True)

        await self.store.save_snapshot(self._snapshot_from_request(event, request))
        await self.store.transition_event(event.event_id, EventState.QUEUED)
        session = ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.PENDING,
        )
        try:
            session = await self.store.create_session(session)
        except ActiveSessionError as exc:
            await self.store.transition_event(
                event.event_id,
                EventState.FAILED,
                details={"reason": "owner_channel_busy"},
            )
            return ContactHumanResult(
                event_id=event.event_id,
                status="failed",
                outcome="none",
                channel="none",
                failure_reason=str(exc),
                created_at=event.detected_at,
            )

        await self.store.transition_event(event.event_id, EventState.DIALING)
        await self.store.transition_session(session.session_id, SessionState.DIALING)
        try:
            attempt = await self.provider.place_call(event.event_id, request)
            session = await self.store.link_attempt(session.session_id, attempt.attempt_id)
        except Exception as exc:
            safe_detail = self._sanitize(str(exc), 400)
            reason = f"{type(exc).__name__}: {safe_detail}"
            await self.store.transition_session(
                session.session_id,
                SessionState.FAILED,
                failure_reason=reason,
            )
            await self.store.transition_event(
                event.event_id,
                EventState.FAILED,
                details={"reason": "provider_failure"},
            )
            self._signal(event.event_id)
            return ContactHumanResult(
                event_id=event.event_id,
                status="failed",
                outcome="none",
                channel="none",
                failure_reason=reason,
                created_at=event.detected_at,
            )

        if not request.wait_for_decision:
            return ContactHumanResult(
                event_id=event.event_id,
                status="calling",
                attempt_id=session.attempt_id,
                created_at=event.detected_at,
            )

        result = await self._wait_for_result(
            event.event_id,
            timeout_seconds=request.timeout_seconds,
        )
        if result is not None:
            return result
        return ContactHumanResult(
            event_id=event.event_id,
            status="timed_out",
            outcome="none",
            attempt_id=session.attempt_id,
            created_at=event.detected_at,
        )

    async def list_events(self, limit: int = 20) -> list[EventSummary]:
        events = await self.store.list_events(limit=limit)
        result: list[EventSummary] = []
        for event in events:
            sessions = await self.store.list_sessions(event_id=event.event_id, limit=1)
            decision = await self.store.get_decision(event.event_id)
            result.append(
                EventSummary(
                    event_id=event.event_id,
                    source=event.source.value,
                    kind=event.kind.value,
                    severity=event.severity.value,
                    summary=event.summary,
                    state=event.state.value,
                    created_at=event.detected_at,
                    attempt_id=sessions[0].attempt_id if sessions else None,
                    decision_id=decision.decision_id if decision else None,
                )
            )
        return result

    async def get_event_detail(self, event_id: str) -> dict[str, JsonValue]:
        event = await self.store.require_event(event_id)
        snapshot = await self.store.get_snapshot(event_id)
        sessions = await self.store.list_sessions(event_id=event_id, limit=10)
        decision = await self.store.get_decision(event_id)
        timeline = await self.store.list_timeline(event_id=event_id, limit=100)
        return {
            "event": event.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "sessions": [item.model_dump(mode="json") for item in sessions],
            "decision": decision.model_dump(mode="json") if decision else None,
            "timeline": [item.model_dump(mode="json") for item in timeline],
        }

    async def escalation_context(
        self, request: EscalationContextRequest
    ) -> EscalationContextResponse:
        event = await self._require_live_voice_read_session(request.event_id)
        snapshot = await self.store.get_snapshot(event.event_id)
        decision = await self.store.get_decision(event.event_id)
        context = self._context_packet(event, snapshot)
        proposed = [
            {
                "action_type": item.id,
                "summary": item.label,
                "risk": _contract_risk(item.risk),
                "parameters": {},
            }
            for item in event.proposed_actions
        ]
        question = event.question or "No response is required."
        spoken = sanitize_untrusted_text(
            f"{event.summary} The agent asks: {question}",
            max_chars=3900,
            known_secrets=self._known_secrets(),
        )
        return EscalationContextResponse(
            event_id=event.event_id,
            summary=event.summary,
            question=question,
            severity=event.severity.value,
            context=context,
            proposed_actions=proposed,
            decision_status=(
                "confirmed"
                if decision and decision.approved_action_ids
                else "recorded"
                if decision
                else "pending"
            ),
            spoken_brief=spoken,
        )

    async def record_instruction(
        self, request: RecordInstructionRequest
    ) -> RecordInstructionResponse:
        event = await self.store.require_event(request.event_id)
        outcome = DecisionOutcome(request.outcome)
        instruction = sanitize_untrusted_text(
            request.instruction,
            max_chars=3000,
            known_secrets=self._known_secrets(),
        )
        constraints = [
            sanitize_untrusted_text(
                item,
                max_chars=500,
                known_secrets=self._known_secrets(),
            )
            for item in request.constraints
        ]
        existing = await self.store.get_decision(event.event_id)
        if existing is not None:
            if not _same_recorded_request(
                existing,
                outcome=outcome,
                instruction=instruction,
                constraints=constraints,
                approved_action_ids=request.approved_action_ids,
            ):
                raise DecisionAlreadyExistsError(
                    f"event {event.event_id!r} already has a different decision"
                )
            return self._record_instruction_response(existing)
        expected_direction = (
            ContactDirection.INBOUND_CONTROL
            if event.evidence.get("caller_allowlisted") is True
            and event.evidence.get("direction") == "inbound"
            else ContactDirection.OUTBOUND_ESCALATION
        )
        session_id = await self._require_live_session(
            event,
            expected_direction=expected_direction,
            require_provider_correlation=True,
            allowed_states=(
                {SessionState.CONNECTED, SessionState.DISCUSSING}
                if expected_direction is ContactDirection.INBOUND_CONTROL
                else None
            ),
        )
        if await self.store.repository_context_was_exposed(event.event_id):
            raise PermissionError(
                "repository evidence events cannot record an authoritative decision"
            )
        identity_verified = self._owner_pin_verified(request.confirmation_pin)
        if not identity_verified:
            raise PermissionError("owner second-factor verification failed")
        for action_id in request.approved_action_ids:
            action = await self.store.get_action(action_id)
            if (
                action is None
                or action.event_id != event.event_id
                or action.state.value != "confirmed"
            ):
                raise ValueError(f"action {action_id!r} is not confirmed for this event")
        decision = Decision(
            event_id=event.event_id,
            session_id=session_id,
            outcome=outcome,
            instruction=instruction,
            constraints=constraints,
            approved_action_ids=request.approved_action_ids,
            identity_verified=identity_verified,
            source=DecisionSource.MID_CALL_TOOL,
        )
        try:
            decision = await self.store.record_decision(decision)
        except DecisionAlreadyExistsError:
            existing = await self.store.get_decision(event.event_id)
            if existing is None:
                raise
            if not _same_decision_intent(existing, decision):
                raise
            decision = existing
        self._signal(event.event_id)
        return self._record_instruction_response(decision)

    @staticmethod
    def _record_instruction_response(decision: Decision) -> RecordInstructionResponse:
        return RecordInstructionResponse(
            accepted=True,
            event_id=decision.event_id,
            decision_id=decision.decision_id,
            message_to_user="Your decision is saved and the waiting agent has been notified.",
            message_to_agent=(decision.instruction or f"Owner decision: {decision.outcome.value}."),
        )

    async def prepare_action(self, request: PrepareActionRequest) -> PrepareActionResponse:
        event = await self.store.require_event(request.event_id)
        session_id = await self._require_live_voice_session(event)
        expires_at = utc_now() + timedelta(minutes=2)
        resolved_workspace = request.workspace_ref or event.workspace
        resolved_thread_id = request.thread_id or event.thread_id
        scope_bindings = {
            "event_id": event.event_id,
            "host_id": event.host_id,
            "workspace": resolved_workspace,
            "thread_id": resolved_thread_id,
            "state_hash": request.commit_or_state_hash,
        }
        if request.action_type in _THREAD_ACTIONS:
            kind, risk = _THREAD_ACTIONS[request.action_type]
            parameters = _validate_thread_parameters(request.action_type, request.parameters)
            digest = action_hash(
                request.action_type,
                parameters,
                bindings=scope_bindings,
            )
            impact = _thread_action_impact(request.action_type, parameters)
        else:
            preview = self.runbooks.preview(request.action_type, request.parameters)
            kind = ActionKind.REGISTERED_RUNBOOK
            risk = RiskLevel(preview.risk.value)
            parameters = {
                "input": preview.normalized_parameters,
                "runbook_action_hash": preview.action_hash,
            }
            digest = action_hash(
                "registered_runbook",
                preview.normalized_parameters,
                bindings={
                    **scope_bindings,
                    "runbook_action_hash": preview.action_hash,
                },
            )
            impact = preview.impact

        # The phrase is derived from the immutable action hash, so a changed
        # target or parameter set necessarily requires a new readback.
        phrase = _confirmation_phrase(digest)
        prepared = PreparedAction(
            event_id=event.event_id,
            session_id=session_id,
            kind=kind,
            target=request.action_type,
            parameters=parameters,
            scope=ActionScope(
                host_id=event.host_id,
                workspace=resolved_workspace,
                thread_id=resolved_thread_id,
                commit=request.commit_or_state_hash,
            ),
            risk=risk,
            action_hash=digest,
            confirmation_phrase_hash=_phrase_hash(phrase),
            requires_confirmation=True,
            expires_at=expires_at,
        )
        prepared = await self.store.prepare_action(prepared)
        nonce = self._signer.issue_nonce(
            subject=prepared.action_id,
            action_hash=prepared.action_hash,
            ttl_seconds=max(
                1,
                min(120, int((prepared.expires_at - utc_now()).total_seconds())),
            ),
            claims={"event_id": event.event_id},
        )
        return PrepareActionResponse(
            action_id=prepared.action_id,
            action_hash=prepared.action_hash,
            confirmation_nonce=nonce,
            risk=_contract_risk(prepared.risk.value),
            exact_readback=f"{impact} To confirm, say exactly: {phrase}",
            expires_at=prepared.expires_at,
        )

    async def confirm_action(self, request: ConfirmActionRequest) -> ConfirmActionResponse:
        action = await self.store.get_action(request.action_id)
        if action is None or action.event_id != request.event_id:
            raise NotFoundError("prepared action does not match this event")
        event = await self.store.require_event(action.event_id)
        await self._require_live_voice_session(
            event,
            expected_session_id=action.session_id,
        )
        if await self.store.repository_context_was_exposed(event.event_id):
            raise PermissionError("repository evidence events cannot authorize an action")
        if not self._owner_pin_verified(request.confirmation_pin):
            return ConfirmActionResponse(
                confirmed=False,
                action_id=request.action_id,
                message_to_user=(
                    "Owner second-factor verification failed. No action was authorized."
                ),
            )
        self._signer.verify_nonce(
            request.confirmation_nonce,
            subject=action.action_id,
            action_hash=action.action_hash,
            expected_claims={"event_id": action.event_id},
            consume=False,
        )
        presented_hash = _phrase_hash(request.exact_confirmation)
        grant = await self.store.confirm_action(
            action.action_id,
            owner_ref="owner_primary",
            confirmation_method=_CONFIRMATION_MAP[request.confirmation_method],
            confirmation_hash=presented_hash,
        )
        return ConfirmActionResponse(
            confirmed=True,
            action_id=action.action_id,
            grant_id=grant.grant_id,
            expires_at=grant.expires_at,
            message_to_user=(
                "The exact action is authorized once. It has not executed until "
                "the execute action tool succeeds."
            ),
        )

    async def execute_action(self, request: ExecuteActionRequest) -> ExecuteActionResponse:
        action = await self.store.get_action(request.action_id)
        if action is None or action.event_id != request.event_id:
            raise NotFoundError("prepared action does not match this event")
        if action.kind is not ActionKind.REGISTERED_RUNBOOK and self.controller is None:
            raise RuntimeError("Codex thread control is unavailable")
        event = await self.store.require_event(action.event_id)
        await self._require_live_voice_session(
            event,
            expected_session_id=action.session_id,
        )
        await self.store.consume_action(
            request.grant_id,
            action_hash=action.action_hash,
        )
        if action.kind is ActionKind.REGISTERED_RUNBOOK:
            inputs = action.parameters.get("input")
            runbook_hash = action.parameters.get("runbook_action_hash")
            if not isinstance(inputs, dict) or not isinstance(runbook_hash, str):
                raise RuntimeError("prepared runbook action is malformed")
            execution = self.runbooks.execute(
                action.target,
                inputs,
                confirmed_action_hash=runbook_hash,
            )
            return ExecuteActionResponse(
                executed=True,
                action_id=action.action_id,
                grant_id=request.grant_id,
                operation_id=execution.operation_id,
                message_to_user=execution.message,
                result=execution.model_dump(mode="json"),
            )

        result = await self._execute_thread_action(action)
        return ExecuteActionResponse(
            executed=True,
            action_id=action.action_id,
            grant_id=request.grant_id,
            operation_id=f"codex-{action.action_id}",
            message_to_user=f"Codex thread action completed: {result['action']}.",
            result=result,
        )

    async def begin_inbound_session(
        self, request: BeginInboundSessionRequest
    ) -> BeginInboundSessionResponse:
        configured_numbers = [
            self.settings.owner_phone_number.get_secret_value(),
            *self.settings.allowlisted_callers,
        ]
        try:
            allowlist = CallerAllowlist(number for number in configured_numbers if number)
            allowed = allowlist.allows(request.caller_phone_number)
        except ValueError:
            allowed = False
        if not allowed:
            return BeginInboundSessionResponse(
                accepted=False,
                identity_verified=False,
                message_to_user=(
                    "This caller is not allowlisted. No thread or incident data can be disclosed."
                ),
            )
        if request.interaction_id is not None:
            existing_session = await self.store.get_session_by_interaction(request.interaction_id)
            if (
                existing_session is not None
                and existing_session.direction is ContactDirection.INBOUND_CONTROL
                and existing_session.event_id is not None
            ):
                return BeginInboundSessionResponse(
                    accepted=True,
                    event_id=existing_session.event_id,
                    identity_verified=False,
                    message_to_user=(
                        "Caller allowlisting was already recorded for this inbound call; "
                        "approvals and actions still require the configured owner PIN."
                    ),
                )
        event = EscalationEvent(
            source=EventSource.MANUAL,
            agent_type=AgentType.EXTERNAL,
            kind=EscalationKind.STATUS,
            severity=Severity.INFO,
            summary="An allowlisted caller initiated an inbound Agent Hotline control call.",
            question="What would you like the agents to do?",
            blocking=False,
            evidence={"caller_allowlisted": True, "direction": "inbound"},
        )
        created = await self.store.create_event(event)
        event = created.event
        await self.store.transition_event(event.event_id, EventState.QUEUED)
        session = ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.INBOUND_CONTROL,
            state=SessionState.CONNECTED,
            interaction_id=request.interaction_id,
        )
        try:
            await self.store.create_session(session)
        except ActiveSessionError:
            await self.store.transition_event(
                event.event_id,
                EventState.FAILED,
                details={"reason": "owner_channel_busy"},
            )
            return BeginInboundSessionResponse(
                accepted=False,
                identity_verified=False,
                message_to_user=(
                    "Another owner call is already active. No task data can be disclosed."
                ),
            )
        await self.store.transition_event(event.event_id, EventState.DIALING)
        await self.store.transition_event(event.event_id, EventState.CONNECTED)
        return BeginInboundSessionResponse(
            accepted=True,
            event_id=event.event_id,
            identity_verified=False,
            message_to_user=(
                "Caller allowlisted. I can list or inspect tasks, but approvals and "
                "actions still require the configured owner PIN."
            ),
        )

    async def list_threads(self, request: ThreadListRequest) -> dict[str, JsonValue]:
        guard_started = time.monotonic()
        try:
            await self._require_live_voice_read_session(request.event_id)
        finally:
            logger.info(
                "voice_list_threads_live_session_guard duration_ms=%.1f",
                (time.monotonic() - guard_started) * 1000,
            )
        if self.controller is None:
            raise RuntimeError("Codex thread control is unavailable")
        query = request.query.strip().casefold() if request.query else None
        status_aliases = _THREAD_STATUS_QUERY_ALIASES.get(query) if query is not None else None
        candidates = await self.controller.list_candidates(
            limit=(
                _VOICE_THREAD_STATUS_QUERY_LIMIT
                if status_aliases is not None
                else (_VOICE_THREAD_QUERY_SCAN_LIMIT if query else request.limit)
            )
        )
        if query:
            if status_aliases is not None:
                candidates = tuple(
                    item for item in candidates if item.status.casefold() in status_aliases
                )
            else:
                candidates = tuple(
                    item
                    for item in candidates
                    if query
                    in " ".join(
                        (
                            item.thread_id,
                            item.name or "",
                            item.preview,
                            Path(item.cwd).name,
                            item.status,
                        )
                    ).casefold()
                )
        return {
            "threads": [
                {
                    "thread_id": item.thread_id,
                    "name": (
                        sanitize_untrusted_text(
                            item.name,
                            max_chars=300,
                            known_secrets=self._known_secrets(),
                        )
                        if item.name is not None
                        else None
                    ),
                    "preview": sanitize_untrusted_text(
                        item.preview,
                        max_chars=500,
                        known_secrets=self._known_secrets(),
                    ),
                    "workspace": sanitize_untrusted_text(
                        Path(item.cwd).name,
                        max_chars=300,
                        known_secrets=self._known_secrets(),
                    ),
                    "status": sanitize_untrusted_text(
                        item.status,
                        max_chars=100,
                        known_secrets=self._known_secrets(),
                    ),
                    "updated_at": item.updated_at,
                }
                for item in candidates[: request.limit]
            ]
        }

    async def inspect_thread(self, request: ThreadInspectRequest) -> dict[str, JsonValue]:
        await self._require_live_voice_read_session(request.event_id)
        if self.controller is None:
            raise RuntimeError("Codex thread control is unavailable")
        response = await self.controller.inspect_thread(request.reference)
        thread = response.get("thread")
        if not isinstance(thread, Mapping):
            raise RuntimeError("Codex returned no thread")
        turns = thread.get("turns")
        turn_summaries: list[dict[str, JsonValue]] = []
        if isinstance(turns, list):
            for turn in turns[-5:]:
                if isinstance(turn, Mapping):
                    turn_summaries.append(
                        {
                            "turn_id": _json_scalar(turn.get("id")),
                            "status": _json_scalar(turn.get("status")),
                        }
                    )
        return {
            "thread_id": _json_scalar(thread.get("id")),
            "name": _json_scalar(thread.get("name")),
            "preview": sanitize_untrusted_text(
                str(thread.get("preview") or ""),
                max_chars=800,
                known_secrets=self._known_secrets(),
            ),
            "workspace": Path(str(thread.get("cwd") or ".")).name,
            "status": _json_scalar(thread.get("status")),
            "recent_turns": turn_summaries,
        }

    async def query_repository(
        self,
        request: RepositoryContextQuery,
    ) -> RepositoryContextResponse:
        """Serve the local MCP/CLI path under local-token authentication."""

        return await asyncio.to_thread(self.repository_context.query, request)

    async def query_repository_for_voice(
        self,
        request: SarvamRepositoryContextRequest,
    ) -> RepositoryContextResponse:
        """Serve a live, event-bound Samvaad query without broad filesystem access."""

        event = await self.store.require_event(request.event_id)
        sessions = await self.store.list_sessions(event_id=event.event_id, limit=5)
        inbound = any(session.direction is ContactDirection.INBOUND_CONTROL for session in sessions)
        if inbound:
            await self._require_allowlisted_inbound(event.event_id)
            session_id = await self._require_live_session(
                event,
                expected_direction=ContactDirection.INBOUND_CONTROL,
                require_provider_correlation=True,
                allowed_states={SessionState.CONNECTED, SessionState.DISCUSSING},
            )
            forced_workspace = None
        else:
            if not any(
                session.direction is ContactDirection.OUTBOUND_ESCALATION for session in sessions
            ):
                raise PermissionError("voice repository context requires a call session")
            session_id = await self._require_live_session(
                event,
                expected_direction=ContactDirection.OUTBOUND_ESCALATION,
                require_provider_correlation=True,
            )
            if not event.workspace:
                raise PermissionError(
                    "outbound repository context requires an event-bound workspace"
                )
            forced_workspace = event.workspace
        if not self._owner_pin_verified(request.confirmation_pin):
            raise PermissionError("repository context requires owner second-factor verification")
        await self.store.append_timeline(
            TimelineEntry(
                event_id=event.event_id,
                session_id=session_id,
                kind=TimelineKind.REPOSITORY_CONTEXT_EXPOSED,
                details={"operation": request.operation},
            )
        )
        query = RepositoryContextQuery.model_validate(
            request.model_dump(mode="python", exclude={"event_id"})
        )
        return await asyncio.to_thread(
            self.repository_context.query,
            query,
            forced_workspace=forced_workspace,
        )

    async def open_secure_fallback(
        self,
        request: FallbackOpenRequest,
    ) -> FallbackOpenResponse:
        """Verify the owner before returning any missed-call event context."""

        try:
            claims = self._fallback_signer.verify(
                request.token.get_secret_value(),
                expected_scope="fallback_open",
            )
        except (InvalidTokenError, ExpiredTokenError) as exc:
            raise PermissionError("secure fallback verification failed") from exc
        event_id = claims.extra.get("event_id")
        if not isinstance(event_id, str):
            raise PermissionError("secure fallback verification failed")
        try:
            fallback = await self.store.verify_fallback(
                claims.subject,
                event_id=event_id,
                pin_matches=self._owner_pin_verified(request.confirmation_pin),
                max_attempts=self.settings.hotline_fallback_max_pin_attempts,
            )
        except FallbackLinkError as exc:
            self._signal(event_id)
            raise PermissionError("secure fallback verification failed") from exc
        event = await self.store.require_event(event_id)
        if event.state is not EventState.FALLBACK_PENDING:
            raise PermissionError("secure fallback is no longer available")
        snapshot = await self.store.get_snapshot(event_id)
        remaining = max(1, int((fallback.expires_at - utc_now()).total_seconds()))
        submission_token = self._fallback_signer.issue(
            subject=fallback.fallback_id,
            scope="fallback_decide",
            ttl_seconds=min(remaining, self.settings.hotline_fallback_ttl_seconds),
            claims={"event_id": event.event_id},
        )
        pending_summary = event.pending_request.get("pending_action_summary")
        owner_constraints = event.pending_request.get("owner_constraints")
        return FallbackOpenResponse(
            submission_token=submission_token,
            summary=self._sanitize(event.summary, 1900),
            question=self._sanitize(
                event.question or "Choose how the waiting agent should proceed.",
                1900,
            ),
            severity=_fallback_contract_severity(event.severity),
            pending_action_summary=(
                self._sanitize(str(pending_summary), 1900)
                if isinstance(pending_summary, str) and pending_summary
                else (
                    self._sanitize(snapshot.pending_action.action, 1900)
                    if snapshot and snapshot.pending_action
                    else None
                )
            ),
            owner_constraints=(
                [self._sanitize(item, 480) for item in owner_constraints if isinstance(item, str)][
                    :20
                ]
                if isinstance(owner_constraints, list)
                else []
            ),
            allowed_outcomes=[
                "approve",
                "deny",
                "instruct",
                "defer",
                "auth_completed",
            ],
            expires_at=fallback.expires_at,
        )

    async def record_secure_fallback_decision(
        self,
        request: FallbackDecisionRequest,
    ) -> FallbackDecisionResponse:
        """Consume a short-lived submission capability and wake the waiting agent."""

        try:
            claims = self._fallback_signer.verify(
                request.submission_token.get_secret_value(),
                expected_scope="fallback_decide",
            )
        except (InvalidTokenError, ExpiredTokenError) as exc:
            raise PermissionError("secure fallback submission failed") from exc
        event_id = claims.extra.get("event_id")
        if not isinstance(event_id, str):
            raise PermissionError("secure fallback submission failed")
        fallback = await self.store.get_fallback(claims.subject)
        if fallback is None or fallback.event_id != event_id:
            raise PermissionError("secure fallback submission failed")
        instruction = (
            self._sanitize(request.instruction, 2900)
            if request.instruction and request.instruction.strip()
            else _fallback_default_instruction(request.outcome)
        )
        decision = Decision(
            event_id=event_id,
            session_id=fallback.session_id,
            outcome=DecisionOutcome(request.outcome),
            instruction=instruction,
            constraints=[],
            approved_action_ids=[],
            identity_verified=True,
            channel=ContactChannel.WEB,
            source=DecisionSource.SECURE_FALLBACK,
        )
        decision = await self.store.record_fallback_decision(
            fallback.fallback_id,
            decision,
        )
        self._signal(event_id)
        return FallbackDecisionResponse(
            accepted=True,
            event_id=event_id,
            decision_id=decision.decision_id,
            message_to_user=(
                "Your response was saved once and the waiting agent was notified. "
                "This link cannot authorize a registered action."
            ),
        )

    async def expire_secure_fallbacks(self) -> int:
        event_ids = await self.store.expire_fallbacks()
        for event_id in event_ids:
            self._signal(event_id)
        return len(event_ids)

    async def reconcile_webhook(self, payload: InstantOutboundWebhook) -> dict[str, JsonValue]:
        metadata: dict[str, JsonValue] = {}
        webhook_config = payload.webhook_config or {}
        nested_metadata = webhook_config.get("metadata")
        if isinstance(nested_metadata, dict):
            metadata = redact_secrets(
                nested_metadata,
                known_secrets=self._known_secrets(),
                redact_phone_numbers=True,
            )
        normalized = SarvamWebhookPayload(
            webhook_id=_provider_webhook_id(payload),
            attempt_id=payload.attempt_id,
            interaction_id=payload.interaction_id,
            status=(
                WebhookStatus.COMPLETED
                if payload.status == "connected"
                else WebhookStatus(payload.status)
            ),
            duration_seconds=payload.duration,
            failure_reason=(
                self._sanitize(payload.failure_reason, 900) if payload.failure_reason else None
            ),
            final_agent_variables=redact_secrets(
                payload.final_agent_variables or {},
                known_secrets=self._known_secrets(),
                redact_phone_numbers=True,
            ),
            transcript=[
                TranscriptTurn(
                    role=(TranscriptRole.AGENT if turn.role == "agent" else TranscriptRole.OWNER),
                    text=sanitize_untrusted_text(
                        turn.en_text,
                        max_chars=3900,
                        known_secrets=self._known_secrets(),
                    ),
                )
                for turn in (payload.interaction_transcript or [])
                if turn.en_text.strip()
            ],
            metadata=metadata,
        )
        receipt = await self.store.record_webhook(normalized)
        if not receipt.created:
            if receipt.event_id:
                self._signal(receipt.event_id)
            return {
                "accepted": True,
                "created": False,
                "event_id": receipt.event_id,
                "status": receipt.status.value,
            }
        if receipt.event_id:
            decision = await self.store.get_decision(receipt.event_id)
            event = await self.store.get_event(receipt.event_id)
            if (
                decision is None
                and event is not None
                and event.state
                not in {
                    EventState.FAILED,
                    EventState.EXPIRED,
                    EventState.RESOLVED,
                }
            ):
                fallback_delivered = event.blocking and await self._deliver_secure_fallback(
                    event,
                    session_id=receipt.session_id,
                    reason=f"call_{normalized.status.value}_without_decision",
                )
                if not fallback_delivered:
                    final_state = (
                        EventState.RESOLVED
                        if not event.blocking
                        and normalized.status in {WebhookStatus.CONNECTED, WebhookStatus.COMPLETED}
                        else EventState.FAILED
                    )
                    with contextlib.suppress(InvalidStateTransitionError):
                        await self.store.transition_event(
                            event.event_id,
                            final_state,
                            details={
                                "reason": (
                                    "notification_delivered"
                                    if final_state is EventState.RESOLVED
                                    else f"call_{payload.status}_without_decision"
                                )
                            },
                        )
            self._signal(receipt.event_id)
        return {
            "accepted": True,
            "created": receipt.created,
            "event_id": receipt.event_id,
            "status": receipt.status.value,
        }

    async def _deliver_secure_fallback(
        self,
        event: EscalationEvent,
        *,
        session_id: str,
        reason: str,
    ) -> bool:
        if (
            self.fallback_notifier is None
            or not self.settings.public_base_url
            or not self.settings.owner_confirmation_pin.get_secret_value()
        ):
            return False
        now = utc_now()
        fallback: FallbackLink | None = None
        try:
            fallback = await self.store.create_fallback(
                FallbackLink(
                    event_id=event.event_id,
                    session_id=session_id,
                    reason=reason,
                    expires_at=now + timedelta(seconds=self.settings.hotline_fallback_ttl_seconds),
                )
            )
            token = self._fallback_signer.issue(
                subject=fallback.fallback_id,
                scope="fallback_open",
                ttl_seconds=self.settings.hotline_fallback_ttl_seconds,
                claims={"event_id": event.event_id},
            )
            secure_url = f"{self.settings.public_base_url}/fallback#{token}"
            await self.fallback_notifier.send(
                FallbackNotification(
                    fallback_id=fallback.fallback_id,
                    event_id=event.event_id,
                    secure_url=SecretStr(secure_url),
                    expires_at=fallback.expires_at,
                )
            )
            await self.store.activate_fallback(fallback.fallback_id)
        except Exception as exc:
            if fallback is not None:
                with contextlib.suppress(Exception):
                    await self.store.fail_fallback_delivery(fallback.fallback_id)
            logger.warning("Secure fallback delivery failed: %s", type(exc).__name__)
            return False
        return True

    async def _execute_thread_action(self, action: PreparedAction) -> dict[str, JsonValue]:
        assert self.controller is not None
        parameters = action.parameters
        if action.target == "thread.instruct":
            result = await self.controller.send_instruction(
                str(parameters["reference"]),
                str(parameters["instruction"]),
            )
        elif action.target == "thread.interrupt":
            result = await self.controller.interrupt(
                str(parameters["reference"]),
                turn_id=(
                    str(parameters["turn_id"]) if parameters.get("turn_id") is not None else None
                ),
            )
        elif action.target == "thread.spawn_root":
            result = await self.controller.spawn_root(
                task=str(parameters["task"]),
                cwd=str(parameters["cwd"]),
            )
        elif action.target == "thread.archive":
            result = await self.controller.archive(
                str(parameters["reference"]),
                confirmed_thread_id=str(parameters["confirmed_thread_id"]),
            )
        else:
            raise ValueError("unsupported thread action")
        return {
            "action": result.action,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
        }

    async def _wait_for_result(
        self,
        event_id: str,
        *,
        timeout_seconds: int,
    ) -> ContactHumanResult | None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        waiter = self._waiters.setdefault(event_id, asyncio.Event())
        try:
            while True:
                result = await self._result_for_event(event_id)
                if result.status in {
                    "resolved",
                    "no_answer",
                    "busy",
                    "failed",
                    "deferred",
                }:
                    return result
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=min(remaining, 1.0))
                except TimeoutError:
                    continue
                waiter.clear()
        finally:
            if not waiter.is_set():
                self._waiters.pop(event_id, None)

    async def _result_for_event(
        self,
        event_id: str,
        *,
        duplicate: bool = False,
    ) -> ContactHumanResult:
        event = await self.store.require_event(event_id)
        decision = await self.store.get_decision(event_id)
        sessions = await self.store.list_sessions(event_id=event_id, limit=1)
        session = sessions[0] if sessions else None
        if decision is not None:
            return ContactHumanResult(
                event_id=event.event_id,
                status=("deferred" if decision.outcome is DecisionOutcome.DEFER else "resolved"),
                outcome=decision.outcome.value,
                instruction=decision.instruction,
                constraints=decision.constraints,
                approved_action_ids=decision.approved_action_ids,
                identity_verified=decision.identity_verified,
                decision_id=decision.decision_id,
                attempt_id=session.attempt_id if session else None,
                created_at=event.detected_at,
                decision_recorded_at=decision.decided_at,
            )
        if event.state is EventState.FALLBACK_PENDING:
            return ContactHumanResult(
                event_id=event.event_id,
                status="fallback_pending",
                attempt_id=session.attempt_id if session else None,
                failure_reason=(
                    "The call ended without a decision; a secure one-time fallback "
                    "was delivered to the owner."
                ),
                created_at=event.detected_at,
            )
        if event.state is EventState.FAILED and session is not None:
            status_by_session = {
                SessionState.NO_ANSWER: "no_answer",
                SessionState.BUSY: "busy",
                SessionState.FAILED: "failed",
                SessionState.CANCELLED: "failed",
                SessionState.COMPLETED: "failed",
            }
            status = status_by_session.get(session.state)
            if status:
                return ContactHumanResult(
                    event_id=event.event_id,
                    status=status,
                    attempt_id=session.attempt_id,
                    failure_reason=(
                        session.failure_reason
                        or (
                            "Call ended without an authoritative saved decision."
                            if session.state is SessionState.COMPLETED
                            else None
                        )
                    ),
                    created_at=event.detected_at,
                )
        if event.state is EventState.FAILED:
            return ContactHumanResult(
                event_id=event.event_id,
                status="failed",
                attempt_id=session.attempt_id if session else None,
                failure_reason="Escalation failed before a decision was saved.",
                created_at=event.detected_at,
            )
        return ContactHumanResult(
            event_id=event.event_id,
            status=(
                "duplicate"
                if duplicate
                else "connected"
                if event.state in {EventState.CONNECTED, EventState.AWAITING_DECISION}
                else "calling"
                if event.state is EventState.DIALING
                else "queued"
            ),
            attempt_id=session.attempt_id if session else None,
            created_at=event.detected_at,
        )

    def _event_from_request(self, request: ContactHumanRequest) -> EscalationEvent:
        context = request.context
        source = _SOURCE_MAP.get(request.source, EventSource.MANUAL)
        agent_type = (
            AgentType.CODEX
            if request.source.startswith("codex")
            else AgentType.CLAUDE
            if request.source.startswith("claude")
            else AgentType.EXTERNAL
        )
        summary = self._sanitize(request.summary, 1900)
        question = self._sanitize(request.question, 1900)
        refs = [
            ContextReference(
                type=item.kind,
                value=self._sanitize(item.ref, 480),
            )
            for item in context.evidence
        ]
        proposed = [
            ProposedAction(
                id=item.action_type,
                label=self._sanitize(item.summary, 480),
                risk=item.risk,
            )
            for item in request.proposed_actions
        ]
        evidence = {
            "task_summary": self._sanitize(context.task_summary, 1400)
            if context.task_summary
            else None,
            "agent_summary": self._sanitize(context.agent_summary, 2400)
            if context.agent_summary
            else None,
            "diff_summary": self._sanitize(context.diff_summary, 2400)
            if context.diff_summary
            else None,
            "test_summary": self._sanitize(context.test_summary, 2000)
            if context.test_summary
            else None,
            "last_error": self._sanitize(context.last_error, 2000) if context.last_error else None,
        }
        evidence = {key: value for key, value in evidence.items() if value is not None}
        return EscalationEvent(
            source=source,
            agent_type=agent_type,
            thread_id=context.thread_id,
            workspace=context.workspace_ref,
            kind=_CONTACT_KIND_MAP[request.kind],
            severity=_model_severity(request.severity),
            summary=summary,
            question=question,
            proposed_actions=proposed,
            context_refs=refs,
            evidence=evidence,
            pending_request={
                "owner_constraints": [
                    self._sanitize(item, 480) for item in context.owner_constraints
                ],
                "pending_action_summary": (
                    self._sanitize(context.pending_action_summary, 1400)
                    if context.pending_action_summary
                    else None
                ),
            },
            blocking=request.wait_for_decision,
            no_answer_policy=_NO_ANSWER_MAP[request.no_answer_policy],
            deadline_at=request.deadline,
            dedupe_key=request.dedupe_key,
        )

    def _snapshot_from_request(
        self,
        event: EscalationEvent,
        request: ContactHumanRequest,
    ) -> ContextSnapshot:
        context = request.context
        files = [self._sanitize(item.ref, 480) for item in context.evidence if item.kind == "file"][
            :50
        ]
        diff_summary = [self._sanitize(context.diff_summary, 480)] if context.diff_summary else []
        pending = (
            PendingActionSnapshot(
                action=self._sanitize(context.pending_action_summary, 900),
                next_step=request.question,
            )
            if context.pending_action_summary
            else None
        )
        return ContextSnapshot(
            event_id=event.event_id,
            agent=event.agent_type,
            task=self._sanitize(context.task_summary or event.summary, 1400),
            agent_summary=self._sanitize(context.agent_summary or event.summary, 2400),
            changed_files=files,
            diff_summary=diff_summary,
            pending_action=pending,
            human_constraints=[self._sanitize(item, 480) for item in context.owner_constraints],
            evidence={
                "test_summary": self._sanitize(context.test_summary, 1900)
                if context.test_summary
                else None,
                "last_error": self._sanitize(context.last_error, 1900)
                if context.last_error
                else None,
            },
        )

    def _context_packet(
        self,
        event: EscalationEvent,
        snapshot: ContextSnapshot | None,
    ) -> ContextPacket:
        evidence = [
            {
                "kind": (
                    reference.type
                    if reference.type
                    in {"diff", "test", "log", "error", "file", "metric", "thread", "other"}
                    else "other"
                ),
                "ref": reference.value,
                "summary": "Context captured by the originating agent.",
            }
            for reference in event.context_refs
        ]
        return ContextPacket(
            thread_id=event.thread_id,
            workspace_ref=event.workspace,
            task_summary=snapshot.task if snapshot else event.summary,
            agent_summary=snapshot.agent_summary if snapshot else event.summary,
            diff_summary=(
                "; ".join(snapshot.diff_summary) if snapshot and snapshot.diff_summary else None
            ),
            test_summary=(
                str(snapshot.evidence.get("test_summary"))
                if snapshot and snapshot.evidence.get("test_summary")
                else None
            ),
            last_error=(
                str(snapshot.evidence.get("last_error"))
                if snapshot and snapshot.evidence.get("last_error")
                else None
            ),
            pending_action_summary=(
                snapshot.pending_action.action if snapshot and snapshot.pending_action else None
            ),
            owner_constraints=snapshot.human_constraints if snapshot else [],
            evidence=evidence,
        )

    async def _require_live_session(
        self,
        event: EscalationEvent,
        *,
        expected_session_id: str | None = None,
        expected_direction: ContactDirection | None = None,
        require_provider_correlation: bool = False,
        allowed_states: set[SessionState] | None = None,
    ) -> str:
        if event.state not in {
            EventState.DIALING,
            EventState.CONNECTED,
            EventState.AWAITING_DECISION,
        }:
            raise PermissionError("action confirmation requires an active escalation")
        sessions = await self.store.list_sessions(event_id=event.event_id, limit=5)
        live_states = allowed_states or {
            SessionState.DIALING,
            SessionState.RINGING,
            SessionState.CONNECTED,
            SessionState.DISCUSSING,
        }
        session = next(
            (
                item
                for item in sessions
                if item.state in live_states
                and (expected_session_id is None or item.session_id == expected_session_id)
                and (expected_direction is None or item.direction is expected_direction)
                and (
                    not require_provider_correlation
                    or (
                        item.direction is ContactDirection.OUTBOUND_ESCALATION
                        and item.attempt_id is not None
                    )
                    or (
                        item.direction is ContactDirection.INBOUND_CONTROL
                        and item.interaction_id is not None
                    )
                )
            ),
            None,
        )
        if session is None:
            raise PermissionError("action confirmation requires a live call session")
        return session.session_id

    async def _require_allowlisted_inbound(self, event_id: str) -> EscalationEvent:
        event = await self.store.require_event(event_id)
        sessions = await self.store.list_sessions(event_id=event_id, limit=5)
        if event.evidence.get("caller_allowlisted") is not True or not any(
            session.direction is ContactDirection.INBOUND_CONTROL for session in sessions
        ):
            raise PermissionError("allowlisted inbound control session required")
        return event

    async def _require_live_voice_read_session(self, event_id: str) -> EscalationEvent:
        """Authorize read-only task discovery only while its voice session is live."""

        event = await self.store.require_event(event_id)
        await self._require_live_voice_session(event)
        return event

    async def _require_live_voice_session(
        self,
        event: EscalationEvent,
        *,
        expected_session_id: str | None = None,
    ) -> str:
        """Require a provider-correlated voice session for a public tool request."""

        inbound = (
            event.evidence.get("caller_allowlisted") is True
            and event.evidence.get("direction") == "inbound"
        )
        if inbound:
            await self._require_allowlisted_inbound(event.event_id)
            return await self._require_live_session(
                event,
                expected_session_id=expected_session_id,
                expected_direction=ContactDirection.INBOUND_CONTROL,
                require_provider_correlation=True,
                allowed_states={SessionState.CONNECTED, SessionState.DISCUSSING},
            )
        return await self._require_live_session(
            event,
            expected_session_id=expected_session_id,
            expected_direction=ContactDirection.OUTBOUND_ESCALATION,
            require_provider_correlation=True,
        )

    def _owner_pin_verified(self, submitted: Any) -> bool:
        configured = self.settings.owner_confirmation_pin.get_secret_value()
        if not configured or submitted is None:
            return False
        candidate = submitted.get_secret_value()
        if not candidate:
            return False
        return hmac.compare_digest(
            configured.encode("utf-8"),
            candidate.encode("utf-8"),
        )

    def _signal(self, event_id: str) -> None:
        waiter = self._waiters.get(event_id)
        if waiter is not None:
            waiter.set()

    def _known_secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.settings.sarvam_api_key.get_secret_value(),
                self.settings.hotline_tool_token.get_secret_value(),
                self.settings.hotline_local_token.get_secret_value(),
                self.settings.hotline_callback_token.get_secret_value(),
                self.settings.owner_confirmation_pin.get_secret_value(),
                self.settings.hotline_fallback_webhook_token.get_secret_value(),
            )
            if value
        )

    def _sanitize(self, value: str, max_chars: int) -> str:
        return sanitize_untrusted_text(
            value,
            max_chars=max(64, max_chars),
            known_secrets=self._known_secrets(),
        )


def _model_severity(value: str) -> Severity:
    if value == "info":
        return Severity.INFO
    if value in {"high", "critical"}:
        return Severity.CRITICAL
    return Severity.WARNING


def _fallback_contract_severity(
    value: Severity,
) -> str:
    if value is Severity.INFO:
        return "info"
    if value is Severity.CRITICAL:
        return "critical"
    return "medium"


def _fallback_default_instruction(outcome: str) -> str:
    return {
        "approve": "Approve only the pending request shown in the secure fallback.",
        "deny": "Deny the pending request shown in the secure fallback.",
        "defer": "Defer the pending request and keep the agent safely paused.",
        "auth_completed": "The legitimate authentication handoff is complete.",
    }.get(outcome, "Follow the confirmed secure fallback response.")


def _contract_risk(value: str) -> str:
    if value in {"low", "medium", "high"}:
        return value
    if value == "critical":
        return "high"
    return "medium"


def _confirmation_phrase(digest: str) -> str:
    return f"CONFIRM ACTION {digest[:4].upper()} {digest[4:8].upper()}"


def _phrase_hash(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _provider_webhook_id(payload: InstantOutboundWebhook) -> str:
    """Derive a stable receipt key because Sarvam does not publish a webhook ID."""

    canonical = json.dumps(
        payload.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "whk_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _same_decision_intent(left: Decision, right: Decision) -> bool:
    return (
        left.event_id == right.event_id
        and left.session_id == right.session_id
        and left.outcome is right.outcome
        and left.instruction == right.instruction
        and left.constraints == right.constraints
        and left.approved_action_ids == right.approved_action_ids
        and left.identity_verified is right.identity_verified
        and left.source is right.source
    )


def _same_recorded_request(
    existing: Decision,
    *,
    outcome: DecisionOutcome,
    instruction: str,
    constraints: list[str],
    approved_action_ids: list[str],
) -> bool:
    return (
        existing.outcome is outcome
        and existing.instruction == instruction
        and existing.constraints == constraints
        and existing.approved_action_ids == approved_action_ids
        and existing.source is DecisionSource.MID_CALL_TOOL
    )


def _validate_thread_parameters(
    action_type: str,
    raw: dict[str, Any],
) -> dict[str, JsonValue]:
    allowed: dict[str, tuple[str, ...]] = {
        "thread.instruct": ("reference", "instruction"),
        "thread.interrupt": ("reference", "turn_id"),
        "thread.spawn_root": ("task", "cwd"),
        "thread.archive": ("reference", "confirmed_thread_id"),
    }
    required: dict[str, tuple[str, ...]] = {
        "thread.instruct": ("reference", "instruction"),
        "thread.interrupt": ("reference",),
        "thread.spawn_root": ("task", "cwd"),
        "thread.archive": ("reference", "confirmed_thread_id"),
    }
    unexpected = set(raw) - set(allowed[action_type])
    if unexpected:
        raise ValueError(f"unsupported thread action parameters: {sorted(unexpected)}")
    missing = [key for key in required[action_type] if not raw.get(key)]
    if missing:
        raise ValueError(f"missing thread action parameters: {missing}")
    result: dict[str, JsonValue] = {}
    for key, value in raw.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 12_000:
            raise ValueError(f"thread action parameter {key!r} must be bounded text")
        result[key] = value.strip()
    return result


def _thread_action_impact(action_type: str, parameters: Mapping[str, JsonValue]) -> str:
    if action_type == "thread.instruct":
        return f"Send one instruction to Codex task {parameters['reference']}."
    if action_type == "thread.interrupt":
        return f"Interrupt the active turn in Codex task {parameters['reference']}."
    if action_type == "thread.spawn_root":
        return f"Spawn a new root Codex task in workspace {Path(str(parameters['cwd'])).name}."
    return f"Archive Codex task {parameters['confirmed_thread_id']}."


def _json_scalar(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)[:500]
