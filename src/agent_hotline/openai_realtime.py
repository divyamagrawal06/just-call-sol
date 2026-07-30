"""OpenAI Realtime SIP admission, sideband control, and call-bound tool dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import quote, unquote

import httpx
import websockets
from openai import InvalidWebhookSignatureError, OpenAI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from .contracts import (
    BeginInboundSessionRequest,
    ConfirmActionRequest,
    EscalationContextRequest,
    ExecuteActionRequest,
    PrepareActionRequest,
    RecordInstructionRequest,
    RepositoryContextQuery,
    ThreadInspectRequest,
    ThreadListRequest,
    VoiceRepositoryContextRequest,
)
from .coordinator import HotlineCoordinator
from .models import (
    TERMINAL_EVENT_STATES,
    TERMINAL_SESSION_STATES,
    CallTerminationJob,
    CallTerminationLeg,
    CallTerminationState,
    ContactDirection,
    ContactSession,
    EventState,
    ProviderWebhookPayload,
    SessionState,
    TimelineEntry,
    TimelineKind,
    WebhookStatus,
    new_id,
    utc_now,
)
from .realtime_prompt import Direction, build_realtime_session_config
from .security import PhoneNumberError, canonical_json, normalize_e164, sanitize_untrusted_text
from .settings import Settings
from .storage import ActiveSessionError, ConflictError, SQLiteStore, StorageError
from .telephony_security import verify_correlation_signature

logger = logging.getLogger(__name__)

_CALL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,299}$")
_TWILIO_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")
_TWILIO_CALL_SID_RE = re.compile(r"^CA[0-9a-fA-F]{32}$")
_SIP_PHONE_RE = re.compile(r"(?:sip:|tel:)(\+[1-9]\d{6,14})(?:[@;>\s]|$)", re.IGNORECASE)
_MAX_WEBSOCKET_MESSAGE_BYTES = 1024 * 1024
_MAX_TOOL_ARGUMENT_BYTES = 16 * 1024
_VERIFICATION_WINDOW_SECONDS = 120
_TOOL_EXECUTION_TIMEOUT_SECONDS = 45.0
_DELIVERY_ACK_TIMEOUT_SECONDS = 10.0
_TERMINATION_RETRY_BASE_SECONDS = 1
_TERMINATION_RETRY_MAX_SECONDS = 300
_TERMINATION_RECONCILE_BATCH = 20


class OpenAIRealtimeError(RuntimeError):
    """Base class for the OpenAI Realtime transport."""


class OpenAIWebhookVerificationError(OpenAIRealtimeError):
    """The OpenAI webhook could not be authenticated."""


class OpenAIRealtimeAPIError(OpenAIRealtimeError):
    """A call-control API request failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AmbiguousToolDeliveryError(OpenAIRealtimeError):
    """A sideband send crossed an ACK boundary with an unknown provider outcome."""


class IncomingCallError(OpenAIRealtimeError):
    """A verified incoming call was malformed or not admissible."""

    def __init__(self, message: str, *, sip_status: int = 603) -> None:
        super().__init__(message)
        self.sip_status = sip_status


class RealtimeSocket(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


SocketFactory = Callable[
    [str, Mapping[str, str]],
    contextlib.AbstractAsyncContextManager[RealtimeSocket],
]


@dataclass(frozen=True, slots=True)
class ParsedIncomingCall:
    event_id: str
    call_id: str
    sip_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class IncomingCallResult:
    handled: bool
    accepted: bool
    duplicate: bool
    call_id: str | None = None
    event_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SIPCorrelation:
    direction: Literal["inbound", "outbound"]
    call_sid: str
    event_id: str | None
    caller_phone: str | None
    admission_nonce: str | None = None
    expires_at_epoch: int | None = None


@dataclass(slots=True)
class VerificationWindow:
    scope: Literal["decision", "action", "repository"]
    subject_id: str
    expected_readback: str
    expires_at_monotonic: float
    readback_response_id: str | None = None
    readback_transcript: str = ""
    readback_response_completed: bool = False
    readback_audio_stopped: bool = False
    readback_delivered: bool = False
    owner_speech_active: bool = False
    owner_replied: bool = False
    armed: bool = False
    digits: str = ""
    attempts: int = 0
    verified: bool = False
    consumed: bool = False
    locked: bool = False


class _PrepareDecisionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["approve", "deny", "instruct", "defer", "auth_completed"]
    instruction: str = Field(min_length=1, max_length=3000)
    constraints: list[str] = Field(default_factory=list, max_length=20)
    approved_action_ids: list[str] = Field(default_factory=list, max_length=20)


class _PrepareActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_.-]*$")
    parameters: dict[str, Any] = Field(default_factory=dict)
    workspace_ref: str | None = Field(default=None, max_length=500)
    thread_id: str | None = Field(default=None, max_length=200)


class _RepositoryArgs(RepositoryContextQuery):
    pass


@dataclass(frozen=True, slots=True)
class PreparedDecision:
    confirmation_id: str
    arguments: _PrepareDecisionArgs
    expires_at_monotonic: float


@dataclass(frozen=True, slots=True)
class PreparedRepositoryQuery:
    request_id: str
    query: RepositoryContextQuery
    expires_at_monotonic: float


@dataclass(frozen=True, slots=True)
class PreparedActionState:
    action_id: str
    confirmation_nonce: str
    exact_readback: str
    grant_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolDispatchResult:
    payload: dict[str, Any]
    request_response: bool = True
    finish_after_response: bool = False


@dataclass(slots=True)
class PendingToolOutput:
    output_json: str
    request_response: bool
    delivery_state: str = "pending"


class OpenAIRealtimeClient:
    """Verify OpenAI webhooks and issue Realtime call-control requests."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        webhook_client: OpenAI | None = None,
        api_base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        default_headers = {
            "Authorization": f"Bearer {settings.openai_api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "User-Agent": "agent-hotline/0.2",
        }
        if settings.openai_project_id is not None:
            default_headers["OpenAI-Project"] = settings.openai_project_id
        self._client = client or httpx.AsyncClient(
            base_url=api_base_url,
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers=default_headers,
        )
        self._webhook_client = webhook_client or OpenAI(
            api_key=settings.openai_api_key.get_secret_value() or "not-configured",
            webhook_secret=settings.openai_webhook_secret.get_secret_value() or "not-configured",
        )

    def unwrap_webhook(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        """Verify the exact raw bytes with the official Standard Webhooks helper."""

        try:
            body = raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OpenAIWebhookVerificationError("webhook body is not UTF-8") from exc
        try:
            event = self._webhook_client.webhooks.unwrap(body, headers)
        except InvalidWebhookSignatureError as exc:
            raise OpenAIWebhookVerificationError("webhook signature is invalid") from exc
        except Exception as exc:
            raise OpenAIWebhookVerificationError("webhook verification failed") from exc
        payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
        if not isinstance(payload, dict):
            raise OpenAIWebhookVerificationError("verified webhook payload is malformed")
        return payload

    async def accept_call(self, call_id: str, session: Mapping[str, Any]) -> None:
        await self._request(
            "POST",
            f"/realtime/calls/{_path_call_id(call_id)}/accept",
            json=session,
        )

    async def reject_call(self, call_id: str, *, status_code: int = 603) -> None:
        if status_code not in {400, 403, 404, 480, 481, 486, 488, 603}:
            raise ValueError("unsupported SIP rejection status")
        try:
            await self._request(
                "POST",
                f"/realtime/calls/{_path_call_id(call_id)}/reject",
                json={"status_code": status_code},
            )
        except OpenAIRealtimeAPIError as exc:
            # Webhook redelivery can race provider-side call expiry. An absent
            # call is already rejected for lifecycle purposes.
            if exc.status_code != 404:
                raise

    async def hangup_call(self, call_id: str) -> None:
        try:
            await self._request(
                "POST",
                f"/realtime/calls/{_path_call_id(call_id)}/hangup",
            )
        except OpenAIRealtimeAPIError as exc:
            # Replaying a durable termination after provider cleanup is success:
            # an absent call cannot retain audio or the owner-channel slot.
            if exc.status_code != 404:
                raise

    async def probe(self) -> None:
        model = quote(self.settings.openai_realtime_model, safe="")
        await self._request("GET", f"/models/{model}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
    ) -> None:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise OpenAIRealtimeAPIError(
                f"OpenAI Realtime request failed: {type(exc).__name__}"
            ) from exc
        if 200 <= response.status_code < 300:
            return
        request_id = response.headers.get("x-request-id")
        suffix = f" (request {request_id})" if request_id else ""
        raise OpenAIRealtimeAPIError(
            f"OpenAI Realtime returned HTTP {response.status_code}{suffix}",
            status_code=response.status_code,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        with contextlib.suppress(Exception):
            self._webhook_client.close()

    async def __aenter__(self) -> OpenAIRealtimeClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


@asynccontextmanager
async def _default_socket_factory(
    url: str,
    headers: Mapping[str, str],
) -> AsyncIterator[RealtimeSocket]:
    async with websockets.connect(
        url,
        additional_headers=dict(headers),
        max_size=_MAX_WEBSOCKET_MESSAGE_BYTES,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
    ) as socket:
        yield socket


class RealtimeToolDispatcher:
    """Translate call-bound model tools into existing coordinator operations."""

    def __init__(
        self,
        *,
        settings: Settings,
        coordinator: HotlineCoordinator,
        event_id: str,
        session_id: str,
        direction: Direction,
    ) -> None:
        self.settings = settings
        self.coordinator = coordinator
        self.event_id = event_id
        self.session_id = session_id
        self.direction = direction
        self._verification: VerificationWindow | None = None
        self._failed_pin_attempts = 0
        self._pin_locked = False
        self._decisions: dict[str, PreparedDecision] = {}
        self._actions: dict[str, PreparedActionState] = {}
        self._repositories: dict[str, PreparedRepositoryQuery] = {}

    async def dispatch(self, name: str, arguments: Mapping[str, Any]) -> ToolDispatchResult:
        handlers: dict[str, Callable[[Mapping[str, Any]], Awaitable[ToolDispatchResult]]] = {
            "get_hotline_context": self._get_context,
            "list_available_actions": self._list_available_actions,
            "prepare_decision": self._prepare_decision,
            "arm_owner_verification": self._arm_owner_verification,
            "check_owner_verification": self._check_owner_verification,
            "record_decision": self._record_decision,
            "list_agent_tasks": self._list_agent_tasks,
            "inspect_agent_task": self._inspect_agent_task,
            "prepare_action": self._prepare_action,
            "confirm_action": self._confirm_action,
            "execute_action": self._execute_action,
            "prepare_repository_access": self._prepare_repository_access,
            "query_repository_context": self._query_repository_context,
            "wait_for_user": self._wait_for_user,
            "finish_session": self._finish_session,
        }
        try:
            handler = handlers[name]
        except KeyError as exc:
            raise ValueError(f"unknown Realtime tool {name!r}") from exc
        return await handler(arguments)

    def receive_dtmf(self, keypad_event: str) -> dict[str, Any] | None:
        """Consume DTMF only inside the active verification window.

        The returned status is safe to inject as a trusted server signal. Digits
        never leave this method, enter logs, reach the model, or touch storage.
        """

        window = self._active_verification()
        if (
            self._pin_locked
            or window is None
            or window.consumed
            or window.locked
            or window.verified
        ):
            return None
        if keypad_event == "*":
            window.digits = ""
            return None
        if keypad_event in {"A", "B", "C", "D"}:
            return None
        if keypad_event in "0123456789":
            if len(window.digits) < 12:
                window.digits += keypad_event
            return None
        if keypad_event != "#":
            return None

        configured = self.settings.owner_confirmation_pin.get_secret_value()
        candidate = window.digits
        window.digits = ""
        verified = bool(configured and candidate) and hmac.compare_digest(
            configured.encode(),
            candidate.encode(),
        )
        candidate = ""
        if verified:
            window.verified = True
            window.digits = ""
            return {
                "verified": True,
                "scope": window.scope,
                "subject_id": window.subject_id,
                "message": (
                    "Trusted server signal: keypad verification succeeded for the current "
                    "prepared request. Continue only with that exact request."
                ),
            }
        self._failed_pin_attempts += 1
        window.attempts = self._failed_pin_attempts
        if self._failed_pin_attempts >= self.settings.hotline_voice_pin_max_attempts:
            self._pin_locked = True
            window.locked = True
            return {
                "verified": False,
                "locked": True,
                "scope": window.scope,
                "subject_id": window.subject_id,
                "message": (
                    "Trusted server signal: keypad verification reached its attempt limit. "
                    "No decision or action is authorized."
                ),
            }
        return {
            "verified": False,
            "locked": False,
            "scope": window.scope,
            "subject_id": window.subject_id,
            "message": (
                "Trusted server signal: keypad verification did not match. Ask the owner to "
                "try again using the keypad followed by #. Do not ask them to speak the PIN."
            ),
        }

    async def _get_context(self, arguments: Mapping[str, Any]) -> ToolDispatchResult:
        _require_no_arguments(arguments)
        response = await self.coordinator.escalation_context(
            EscalationContextRequest(event_id=self.event_id)
        )
        return _ok(response.model_dump(mode="json"))

    async def _list_available_actions(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        _require_no_arguments(arguments)
        runbooks = [summary.model_dump(mode="json") for summary in self.coordinator.runbooks.list()]
        thread_actions: list[dict[str, Any]] = []
        if self.coordinator.controller is not None:
            thread_actions = [
                {
                    "action_type": "thread.instruct",
                    "description": "Send an instruction to one exact Codex task.",
                    "required_parameters": ["reference", "instruction"],
                },
                {
                    "action_type": "thread.interrupt",
                    "description": "Interrupt the active turn of one exact Codex task.",
                    "required_parameters": ["reference"],
                },
                {
                    "action_type": "thread.spawn_root",
                    "description": "Create one new root Codex task in the configured workspace.",
                    "required_parameters": ["task", "cwd"],
                },
                {
                    "action_type": "thread.archive",
                    "description": "Archive one exact Codex task after ID confirmation.",
                    "required_parameters": ["reference", "confirmed_thread_id"],
                },
            ]
        return _ok(
            {
                "registered_runbooks": runbooks,
                "codex_task_actions": thread_actions,
                "claude_note": (
                    "Claude supports MCP escalation and hook notifications. Deep task listing "
                    "and control are Codex-only in this runtime."
                ),
            }
        )

    async def _prepare_decision(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        parsed = _PrepareDecisionArgs.model_validate(arguments)
        confirmation_id = new_id("cfm")
        expiry = time.monotonic() + _VERIFICATION_WINDOW_SECONDS
        constraint_text = (
            " Constraints: " + "; ".join(parsed.constraints) + "."
            if parsed.constraints
            else " No additional constraints."
        )
        action_text = (
            " Confirmed action references: " + ", ".join(parsed.approved_action_ids) + "."
            if parsed.approved_action_ids
            else ""
        )
        readback = sanitize_untrusted_text(
            (
                f"I will record the owner's decision as {parsed.outcome}: "
                f"{parsed.instruction}.{constraint_text}{action_text} "
                "If that is exactly right, say yes, then enter your PIN on the keypad "
                "followed by #."
            ),
            max_chars=3900,
            known_secrets=(self.settings.owner_confirmation_pin.get_secret_value(),),
        )
        self._decisions[confirmation_id] = PreparedDecision(
            confirmation_id=confirmation_id,
            arguments=parsed,
            expires_at_monotonic=expiry,
        )
        self._open_verification("decision", confirmation_id, readback)
        return _ok(
            {
                "confirmation_id": confirmation_id,
                "response_text": readback,
                "require_repeat_verbatim": True,
                "expires_in_seconds": _VERIFICATION_WINDOW_SECONDS,
            }
        )

    async def _arm_owner_verification(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        _require_no_arguments(arguments)
        window = self._current_verification()
        if window is None:
            raise PermissionError("no prepared verification request is active")
        if window.consumed or window.locked or window.verified:
            raise PermissionError("the prepared verification request is no longer armable")
        if not window.readback_delivered:
            raise PermissionError("the exact server readback has not been delivered")
        if not window.owner_replied:
            raise PermissionError("the owner has not replied after the exact server readback")
        window.armed = True
        window.digits = ""
        window.expires_at_monotonic = time.monotonic() + _VERIFICATION_WINDOW_SECONDS
        return _ok(
            {
                "armed": True,
                "scope": window.scope,
                "subject_id": window.subject_id,
                "response_text": (
                    "Keypad verification is active for this exact prepared request. "
                    "Ask the owner to enter the PIN followed by #. Never ask for spoken digits."
                ),
            }
        )

    async def _check_owner_verification(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        _require_no_arguments(arguments)
        window = self._current_verification()
        return _ok(
            {
                "active": bool(window and window.armed and not self._pin_locked),
                "prepared": window is not None,
                "readback_delivered": bool(window and window.readback_delivered),
                "owner_replied": bool(window and window.owner_replied),
                "verified": bool(window and window.verified and not window.consumed),
                "locked": self._pin_locked or bool(window and window.locked),
                "scope": window.scope if window else None,
                "subject_id": window.subject_id if window else None,
            }
        )

    async def _record_decision(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        confirmation_id = _required_string(arguments, "confirmation_id", maximum=100)
        _reject_extra_arguments(arguments, {"confirmation_id"})
        prepared = self._decisions.get(confirmation_id)
        if prepared is None or prepared.expires_at_monotonic <= time.monotonic():
            raise PermissionError("prepared decision is missing or expired")
        self._consume_verification("decision", confirmation_id)
        pin = SecretStr(self.settings.owner_confirmation_pin.get_secret_value())
        response = await self.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=self.event_id,
                outcome=prepared.arguments.outcome,
                instruction=prepared.arguments.instruction,
                constraints=prepared.arguments.constraints,
                approved_action_ids=prepared.arguments.approved_action_ids,
                confirmation_method="spoken_plus_dtmf",
                confirmation_pin=pin,
            )
        )
        return _ok(response.model_dump(mode="json"))

    async def _list_agent_tasks(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        _reject_extra_arguments(arguments, {"query", "limit"})
        query = arguments.get("query")
        limit = arguments.get("limit", 10)
        response = await self.coordinator.list_threads(
            ThreadListRequest(
                event_id=self.event_id,
                query=query if isinstance(query, str) else None,
                limit=limit,
            )
        )
        return _ok(response)

    async def _inspect_agent_task(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        reference = _required_string(arguments, "reference", maximum=200)
        _reject_extra_arguments(arguments, {"reference"})
        response = await self.coordinator.inspect_thread(
            ThreadInspectRequest(event_id=self.event_id, reference=reference)
        )
        return _ok(response)

    async def _prepare_action(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        parsed = _PrepareActionArgs.model_validate(arguments)
        response = await self.coordinator.prepare_action(
            PrepareActionRequest(
                event_id=self.event_id,
                **parsed.model_dump(mode="python"),
            )
        )
        self._actions[response.action_id] = PreparedActionState(
            action_id=response.action_id,
            confirmation_nonce=response.confirmation_nonce,
            exact_readback=response.exact_readback,
            grant_id=response.grant_id,
        )
        if not response.executed:
            self._open_verification(
                "action",
                response.action_id,
                response.exact_readback,
            )
        return _ok(
            {
                **response.model_dump(mode="json"),
                "require_repeat_verbatim": not response.executed,
                "pin_instruction": (
                    None
                    if response.executed
                    else "Ask for the keypad PIN followed by #; never ask for spoken digits."
                ),
            }
        )

    async def _confirm_action(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        action_id = _required_string(arguments, "action_id", maximum=100)
        exact = _required_string(arguments, "exact_confirmation", maximum=1000)
        _reject_extra_arguments(arguments, {"action_id", "exact_confirmation"})
        prepared = self._actions.get(action_id)
        if prepared is None:
            raise PermissionError("action was not prepared in this call")
        self._consume_verification("action", action_id)
        response = await self.coordinator.confirm_action(
            ConfirmActionRequest(
                event_id=self.event_id,
                action_id=action_id,
                confirmation_nonce=prepared.confirmation_nonce,
                exact_confirmation=exact,
                confirmation_method="spoken_plus_dtmf",
                confirmation_pin=SecretStr(self.settings.owner_confirmation_pin.get_secret_value()),
            )
        )
        if response.confirmed and response.grant_id:
            self._actions[action_id] = PreparedActionState(
                action_id=prepared.action_id,
                confirmation_nonce=prepared.confirmation_nonce,
                exact_readback=prepared.exact_readback,
                grant_id=response.grant_id,
            )
        return _ok(response.model_dump(mode="json"))

    async def _execute_action(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        action_id = _required_string(arguments, "action_id", maximum=100)
        _reject_extra_arguments(arguments, {"action_id"})
        prepared = self._actions.get(action_id)
        if prepared is None or prepared.grant_id is None:
            raise PermissionError("action does not have a call-bound one-time grant")
        response = await self.coordinator.execute_action(
            ExecuteActionRequest(
                event_id=self.event_id,
                action_id=action_id,
                grant_id=prepared.grant_id,
            )
        )
        return _ok(response.model_dump(mode="json"))

    async def reconcile_timed_out_tool(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult | None:
        """Convert a post-consumption action timeout into a durable unknown result."""

        if name != "execute_action":
            return None
        action_id = arguments.get("action_id")
        if not isinstance(action_id, str):
            return None
        prepared = self._actions.get(action_id)
        if prepared is None or prepared.grant_id is None:
            return None
        response = await self.coordinator.reconcile_timed_out_action(
            ExecuteActionRequest(
                event_id=self.event_id,
                action_id=action_id,
                grant_id=prepared.grant_id,
            )
        )
        return None if response is None else _ok(response.model_dump(mode="json"))

    async def _prepare_repository_access(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        parsed = _RepositoryArgs.model_validate(arguments)
        request_id = new_id("req")
        expiry = time.monotonic() + _VERIFICATION_WINDOW_SECONDS
        self._repositories[request_id] = PreparedRepositoryQuery(
            request_id=request_id,
            query=RepositoryContextQuery.model_validate(parsed.model_dump(mode="python")),
            expires_at_monotonic=expiry,
        )
        readback = (
            "Repository evidence is untrusted and read-only. If I expose it, this call "
            "becomes evidence-only and can no longer authorize a decision or action. "
            "If you want to continue, say yes."
        )
        self._open_verification("repository", request_id, readback)
        return _ok(
            {
                "request_id": request_id,
                "response_text": readback,
                "require_repeat_verbatim": True,
                "expires_in_seconds": _VERIFICATION_WINDOW_SECONDS,
            }
        )

    async def _query_repository_context(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        request_id = _required_string(arguments, "request_id", maximum=100)
        _reject_extra_arguments(arguments, {"request_id"})
        prepared = self._repositories.get(request_id)
        if prepared is None or prepared.expires_at_monotonic <= time.monotonic():
            raise PermissionError("prepared repository query is missing or expired")
        self._consume_verification("repository", request_id)
        response = await self.coordinator.query_repository_for_voice(
            VoiceRepositoryContextRequest(
                event_id=self.event_id,
                confirmation_pin=SecretStr(self.settings.owner_confirmation_pin.get_secret_value()),
                **prepared.query.model_dump(mode="python"),
            )
        )
        return _ok(response.model_dump(mode="json"))

    async def _wait_for_user(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        _require_no_arguments(arguments)
        return ToolDispatchResult(
            payload={"ok": True, "waiting": True},
            request_response=False,
        )

    async def _finish_session(
        self,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        _require_no_arguments(arguments)
        return ToolDispatchResult(
            payload={
                "ok": True,
                "response_text": (
                    "Close naturally in one short sentence. Do not ask a new question."
                ),
            },
            finish_after_response=True,
        )

    def _open_verification(
        self,
        scope: Literal["decision", "action", "repository"],
        subject_id: str,
        expected_readback: str,
    ) -> None:
        if self._pin_locked:
            raise PermissionError("keypad verification is locked for the remainder of this call")
        if self._verification is not None:
            self._verification.digits = ""
            self._verification.consumed = True
        self._verification = VerificationWindow(
            scope=scope,
            subject_id=subject_id,
            expected_readback=expected_readback,
            expires_at_monotonic=time.monotonic() + _VERIFICATION_WINDOW_SECONDS,
        )

    def _current_verification(self) -> VerificationWindow | None:
        window = self._verification
        if window is None:
            return None
        if window.expires_at_monotonic <= time.monotonic():
            window.digits = ""
            window.consumed = True
            return None
        return window

    def _active_verification(self) -> VerificationWindow | None:
        window = self._current_verification()
        if window is None or not window.armed:
            return None
        return window

    def note_readback_transcript(self, response_id: str, transcript: str) -> None:
        """Bind a completed audio transcript to the current prepared readback."""

        window = self._current_verification()
        if window is None or window.consumed or window.armed or not response_id or not transcript:
            return
        if window.readback_response_id is not None and window.readback_response_id != response_id:
            return
        window.readback_response_id = response_id
        window.readback_transcript = transcript

    def note_response_done(self, response_id: str, transcript: str | None = None) -> None:
        """Record exact text completion without assuming phone playback has drained."""

        if transcript:
            self.note_readback_transcript(response_id, transcript)
        window = self._current_verification()
        if (
            window is None
            or window.readback_response_id != response_id
            or not window.readback_transcript
        ):
            return
        window.readback_response_completed = True
        self._update_readback_delivery(window)

    def note_output_audio_stopped(self, response_id: str) -> None:
        """Record that the exact response has fully drained to the SIP audio leg."""

        window = self._current_verification()
        if (
            window is None
            or window.consumed
            or window.armed
            or window.readback_response_id != response_id
        ):
            return
        window.readback_audio_stopped = True
        self._update_readback_delivery(window)

    @staticmethod
    def _update_readback_delivery(window: VerificationWindow) -> None:
        window.readback_delivered = bool(
            window.readback_response_completed
            and window.readback_audio_stopped
            and _spoken_text_matches(
                window.expected_readback,
                window.readback_transcript,
            )
        )

    def note_owner_speech_started(self) -> None:
        """Fail closed on a readback interruption, or bind a later owner reply."""

        window = self._current_verification()
        if window is None or window.armed:
            return
        if not window.readback_delivered:
            window.digits = ""
            window.consumed = True
            return
        window.owner_speech_active = True

    def note_owner_speech_stopped(self) -> None:
        """Record one complete owner speech turn that began after the readback."""

        window = self._current_verification()
        if (
            window is not None
            and window.readback_delivered
            and window.owner_speech_active
            and not window.armed
        ):
            window.owner_replied = True
            window.owner_speech_active = False

    def note_owner_speech_turn(self) -> None:
        """Compatibility helper for tests and non-streaming adapters."""

        self.note_owner_speech_started()
        self.note_owner_speech_stopped()

    def invalidate_verification(self) -> None:
        """Erase every non-consumed keypad window after a transport boundary."""

        window = self._verification
        if window is not None:
            window.digits = ""
            window.consumed = True
            window.armed = False

    def _consume_verification(
        self,
        scope: Literal["decision", "action", "repository"],
        subject_id: str,
    ) -> None:
        window = self._active_verification()
        if (
            self._pin_locked
            or window is None
            or window.scope != scope
            or window.subject_id != subject_id
            or not window.verified
            or window.consumed
            or window.locked
        ):
            raise PermissionError("fresh owner keypad verification is required")
        window.consumed = True
        window.digits = ""


class RealtimeConversation:
    """Own one accepted call's server-side WebSocket and function-call loop."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        client: OpenAIRealtimeClient,
        socket_factory: SocketFactory,
        call_id: str,
        attempt_id: str,
        event_id: str,
        session_id: str,
        direction: Direction,
        coordinator: HotlineCoordinator,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.socket_factory = socket_factory
        self.call_id = call_id
        self.attempt_id = attempt_id
        self.event_id = event_id
        self.session_id = session_id
        self.direction = direction
        self.dispatcher = RealtimeToolDispatcher(
            settings=settings,
            coordinator=coordinator,
            event_id=event_id,
            session_id=session_id,
            direction=direction,
        )
        self._send_lock = asyncio.Lock()
        self._tool_lock = asyncio.Lock()
        self._tool_tasks: set[asyncio.Task[None]] = set()
        self._inflight_tool_calls: set[str] = set()
        self._pending_tool_outputs: dict[str, PendingToolOutput] = {}
        self._fatal_tool_error: Exception | None = None
        self._tool_connection_error: Exception | None = None
        self._tool_connection_failure = asyncio.Event()
        self._item_ack_waiters: dict[str, asyncio.Future[None]] = {}
        self._response_ack_waiters: dict[str, asyncio.Future[None]] = {}
        self._client_event_waiters: dict[str, asyncio.Future[None]] = {}
        self._finish_after_response = False
        self._finish_response_id: str | None = None
        self._stop = asyncio.Event()
        self._stopped_externally = False
        self._socket: RealtimeSocket | None = None

    async def run(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key.get_secret_value()}",
            "User-Agent": "agent-hotline/0.2",
        }
        if self.settings.openai_project_id is not None:
            headers["OpenAI-Project"] = self.settings.openai_project_id
        url = "wss://api.openai.com/v1/realtime?call_id=" + quote(self.call_id, safe="")
        reader: asyncio.Task[None] | None = None
        try:
            async with self.socket_factory(url, headers) as socket:
                self._socket = socket
                reader = asyncio.create_task(
                    self._read_loop(socket),
                    name=f"agent-hotline-realtime-reader-{self.call_id}",
                )
                await self._hydrate_pending_tool_outputs()
                if self._pending_tool_outputs:
                    raise AmbiguousToolDeliveryError(
                        "unfinished Realtime tool delivery cannot be replayed safely"
                    )
                await self._send(
                    socket,
                    {
                        "type": "response.create",
                        "response": {
                            "instructions": (
                                "Begin the call now using the configured opening and "
                                "conversation flow."
                            )
                        },
                    },
                )
                await reader
        finally:
            if reader is not None and not reader.done():
                reader.cancel()
            if reader is not None:
                await asyncio.gather(reader, return_exceptions=True)
            if self._tool_tasks:
                await asyncio.gather(*tuple(self._tool_tasks), return_exceptions=True)
            self.dispatcher.invalidate_verification()
            self._socket = None

    async def stop(self) -> None:
        self._stopped_externally = True
        self._stop.set()
        invalidate = getattr(
            self.dispatcher,
            "invalidate_verification",
            None,
        )
        if callable(invalidate):
            invalidate()
        socket = self._socket
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close(code=1000, reason="call ended")

    async def _read_loop(self, socket: RealtimeSocket) -> None:
        while not self._stop.is_set():
            raw = await self._receive_or_tool_failure(socket)
            if isinstance(raw, bytes):
                if len(raw) > _MAX_WEBSOCKET_MESSAGE_BYTES:
                    raise OpenAIRealtimeError("Realtime event exceeds the message limit")
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise OpenAIRealtimeError("Realtime event is not UTF-8") from exc
            if len(raw.encode("utf-8")) > _MAX_WEBSOCKET_MESSAGE_BYTES:
                raise OpenAIRealtimeError("Realtime event exceeds the message limit")
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Ignored malformed Realtime JSON call_id=%s", self.call_id)
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            if event_type == "error" or (
                isinstance(event_type, str)
                and event_type in {"invalid_request_error", "server_error"}
            ):
                error = event.get("error")
                client_event_id = (
                    error.get("event_id") if isinstance(error, dict) else event.get("event_id")
                )
                error_type = error.get("type") if isinstance(error, dict) else event_type
                logger.warning(
                    "Realtime server error call_id=%s error_type=%s correlated=%s",
                    self.call_id,
                    error_type if isinstance(error_type, str) else "unknown",
                    isinstance(client_event_id, str),
                )
                waiter = (
                    self._client_event_waiters.get(client_event_id)
                    if isinstance(client_event_id, str)
                    else None
                )
                if waiter is not None and not waiter.done():
                    waiter.set_exception(
                        OpenAIRealtimeError("OpenAI Realtime rejected a correlated Hotline event")
                    )
                continue
            if event_type in {
                "conversation.item.created",
                "conversation.item.added",
                "conversation.item.done",
            }:
                item = event.get("item")
                item_id = item.get("id") if isinstance(item, dict) else None
                waiter = self._item_ack_waiters.get(item_id) if isinstance(item_id, str) else None
                if waiter is not None and not waiter.done():
                    waiter.set_result(None)
            if event_type == "response.created":
                response = event.get("response")
                metadata = response.get("metadata") if isinstance(response, dict) else None
                tool_call_id = (
                    metadata.get("hotline_tool_call_id") if isinstance(metadata, dict) else None
                )
                waiter = (
                    self._response_ack_waiters.get(tool_call_id)
                    if isinstance(tool_call_id, str)
                    else None
                )
                if waiter is not None and not waiter.done():
                    waiter.set_result(None)
            if event_type == "response.output_audio_transcript.done":
                response_id = event.get("response_id")
                transcript = event.get("transcript")
                note_transcript = getattr(
                    self.dispatcher,
                    "note_readback_transcript",
                    None,
                )
                if (
                    callable(note_transcript)
                    and isinstance(response_id, str)
                    and isinstance(transcript, str)
                ):
                    note_transcript(response_id, transcript)
                continue
            if event_type == "output_audio_buffer.stopped":
                response_id = event.get("response_id")
                note_output_audio_stopped = getattr(
                    self.dispatcher,
                    "note_output_audio_stopped",
                    None,
                )
                if isinstance(response_id, str) and callable(note_output_audio_stopped):
                    note_output_audio_stopped(response_id)
                if isinstance(response_id, str) and self._finish_response_id == response_id:
                    self._stop.set()
                    with contextlib.suppress(Exception):
                        await socket.close(code=1000, reason="session finished")
                    return
                continue
            if event_type == "input_audio_buffer.speech_started":
                note_owner_speech_started = getattr(
                    self.dispatcher,
                    "note_owner_speech_started",
                    None,
                )
                if callable(note_owner_speech_started):
                    note_owner_speech_started()
                continue
            if event_type == "input_audio_buffer.speech_stopped":
                note_owner_speech_stopped = getattr(
                    self.dispatcher,
                    "note_owner_speech_stopped",
                    None,
                )
                if callable(note_owner_speech_stopped):
                    note_owner_speech_stopped()
                continue
            if event_type == "input_audio_buffer.dtmf_event_received":
                keypad_event = event.get("event")
                if isinstance(keypad_event, str) and len(keypad_event) == 1:
                    status = self.dispatcher.receive_dtmf(keypad_event.upper())
                    if status is not None:
                        await self._send_trusted_signal(socket, status)
                continue

            tool_calls = _extract_function_calls(event)
            if len(tool_calls) > 1:
                await self._reject_parallel_tool_calls(socket, tool_calls)
                continue
            for tool_call_id, name, arguments in tool_calls:
                if tool_call_id in self._inflight_tool_calls:
                    continue
                self._inflight_tool_calls.add(tool_call_id)
                task = asyncio.create_task(
                    self._process_tool_call(
                        socket,
                        tool_call_id=tool_call_id,
                        name=name,
                        arguments_json=arguments,
                    ),
                    name=f"agent-hotline-realtime-tool-{tool_call_id}",
                )
                self._tool_tasks.add(task)
                task.add_done_callback(
                    lambda completed, call_id=tool_call_id: self._tool_task_finished(
                        call_id,
                        completed,
                    )
                )

            if event_type == "response.done":
                response_completed = _response_is_completed(event)
                if response_completed:
                    response_id, transcript = _completed_response_transcript(event)
                    note_response_done = getattr(
                        self.dispatcher,
                        "note_response_done",
                        None,
                    )
                    if response_id is not None and callable(note_response_done):
                        note_response_done(response_id, transcript)
            else:
                response_completed = False
            if (
                event_type == "response.done"
                and response_completed
                and not tool_calls
                and self._finish_after_response
            ):
                response_id, _transcript = _completed_response_transcript(event)
                if response_id is not None:
                    self._finish_response_id = response_id

    async def _process_tool_call(
        self,
        socket: RealtimeSocket,
        *,
        tool_call_id: str,
        name: str,
        arguments_json: str,
    ) -> None:
        async with self._tool_lock:
            try:
                arguments = _load_tool_arguments(arguments_json)
                arguments_hash = hashlib.sha256(canonical_json(arguments).encode()).hexdigest()
            except Exception:
                await self._send_tool_output(
                    socket,
                    tool_call_id,
                    _error_payload("Tool arguments were malformed.", retryable=False),
                    request_response=True,
                )
                return

            existing = await self.store.get_realtime_tool_delivery(
                self.call_id,
                tool_call_id,
            )
            if existing is not None:
                if existing.tool_name != name or not hmac.compare_digest(
                    existing.arguments_hash,
                    arguments_hash,
                ):
                    await self._send_tool_output(
                        socket,
                        tool_call_id,
                        _error_payload(
                            "The tool call identifier was reused with different content.",
                            retryable=False,
                        ),
                        request_response=True,
                    )
                    return
                if existing.tool_name == "finish_session":
                    self._finish_after_response = True
                if existing.delivery_state == "delivered":
                    return
                await self._deliver_tool_output_json(
                    socket,
                    tool_call_id,
                    existing.output_json,
                    request_response=existing.request_response,
                    delivery_state=existing.delivery_state,
                )
                return

            try:
                async with asyncio.timeout(_TOOL_EXECUTION_TIMEOUT_SECONDS):
                    result = await self.dispatcher.dispatch(name, arguments)
            except TimeoutError as exc:
                result = await self.dispatcher.reconcile_timed_out_tool(name, arguments)
                if result is None:
                    raise OpenAIRealtimeError(
                        f"Realtime tool {name!r} exceeded its execution deadline"
                    ) from exc
            except (ValidationError, ValueError) as exc:
                result = ToolDispatchResult(
                    _error_payload(
                        _safe_tool_error(exc, fallback="That request was not valid."),
                        retryable=False,
                    )
                )
            except PermissionError as exc:
                result = ToolDispatchResult(
                    _error_payload(
                        _safe_tool_error(exc, fallback="That operation is not authorized."),
                        retryable=False,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Realtime tool failed call_id=%s tool=%s error_type=%s",
                    self.call_id,
                    name,
                    type(exc).__name__,
                )
                result = ToolDispatchResult(
                    _error_payload("The tool is temporarily unavailable.", retryable=True)
                )

            output_json = json.dumps(
                result.payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            try:
                await self.store.record_realtime_tool_receipt(
                    self.call_id,
                    tool_call_id,
                    tool_name=name,
                    arguments_hash=arguments_hash,
                    output_json=output_json,
                    request_response=result.request_response,
                )
            except ConflictError:
                output_json = json.dumps(
                    _error_payload(
                        "The tool result conflicted with an earlier receipt.",
                        retryable=False,
                    ),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            if result.finish_after_response:
                self._finish_after_response = True
            await self._deliver_tool_output_json(
                socket,
                tool_call_id,
                output_json,
                request_response=result.request_response,
            )

    async def _deliver_tool_output_json(
        self,
        socket: RealtimeSocket,
        tool_call_id: str,
        output_json: str,
        *,
        request_response: bool,
        delivery_state: str = "pending",
    ) -> None:
        delivery = PendingToolOutput(
            output_json=output_json,
            request_response=request_response,
            delivery_state=delivery_state,
        )
        self._pending_tool_outputs[tool_call_id] = delivery
        await self._advance_tool_output_delivery(socket, tool_call_id, delivery)
        if delivery.delivery_state == "delivered":
            self._pending_tool_outputs.pop(tool_call_id, None)

    async def _hydrate_pending_tool_outputs(self) -> None:
        deliveries = await self.store.list_pending_realtime_tool_deliveries(self.call_id)
        self._pending_tool_outputs = {
            tool_call_id: PendingToolOutput(
                output_json=receipt.output_json,
                request_response=receipt.request_response,
                delivery_state=receipt.delivery_state,
            )
            for tool_call_id, receipt in deliveries
        }

    async def _advance_tool_output_delivery(
        self,
        socket: RealtimeSocket,
        tool_call_id: str,
        delivery: PendingToolOutput,
    ) -> None:
        if delivery.delivery_state == "pending":
            item_id = _tool_item_id(tool_call_id)
            output_event_id = _tool_event_id(tool_call_id, "output")
            await self._send_with_delivery_ack(
                socket,
                event={
                    "event_id": output_event_id,
                    "type": "conversation.item.create",
                    "item": {
                        "id": item_id,
                        "type": "function_call_output",
                        "call_id": tool_call_id,
                        "output": delivery.output_json,
                    },
                },
                client_event_id=output_event_id,
                acknowledgement_key=item_id,
                waiters=self._item_ack_waiters,
            )
            delivery.delivery_state = "output_sent" if delivery.request_response else "delivered"
            await self.store.mark_realtime_tool_delivery(
                self.call_id,
                tool_call_id,
                delivery_state=delivery.delivery_state,
            )
        if delivery.request_response and delivery.delivery_state == "output_sent":
            response_event_id = _tool_event_id(tool_call_id, "response")
            response: dict[str, Any] = {
                "event_id": response_event_id,
                "type": "response.create",
                "response": {
                    "metadata": {"hotline_tool_call_id": tool_call_id},
                },
            }
            readback = self._pending_readback_from_output(delivery.output_json)
            if readback is not None:
                response_payload = response["response"]
                assert isinstance(response_payload, dict)
                response_payload["instructions"] = (
                    "Speak the following server-generated readback verbatim and say "
                    "nothing before or after it. Preserve every identifier and qualifier: "
                    + readback
                )
            await self._send_with_delivery_ack(
                socket,
                event=response,
                client_event_id=response_event_id,
                acknowledgement_key=tool_call_id,
                waiters=self._response_ack_waiters,
            )
            delivery.delivery_state = "delivered"
            await self.store.mark_realtime_tool_delivery(
                self.call_id,
                tool_call_id,
                delivery_state="delivered",
            )

    async def _send_with_delivery_ack(
        self,
        socket: RealtimeSocket,
        *,
        event: Mapping[str, Any],
        client_event_id: str,
        acknowledgement_key: str,
        waiters: dict[str, asyncio.Future[None]],
    ) -> None:
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        waiters[acknowledgement_key] = waiter
        self._client_event_waiters[client_event_id] = waiter
        try:
            try:
                await self._send(socket, event)
            except Exception as exc:
                raise AmbiguousToolDeliveryError(
                    "Realtime tool delivery send outcome is unknown"
                ) from exc
            try:
                async with asyncio.timeout(_DELIVERY_ACK_TIMEOUT_SECONDS):
                    await asyncio.shield(waiter)
            except TimeoutError as exc:
                raise AmbiguousToolDeliveryError(
                    "Realtime tool delivery acknowledgement outcome is unknown"
                ) from exc
        finally:
            if waiters.get(acknowledgement_key) is waiter:
                waiters.pop(acknowledgement_key, None)
            if self._client_event_waiters.get(client_event_id) is waiter:
                self._client_event_waiters.pop(client_event_id, None)

    def _tool_task_finished(
        self,
        tool_call_id: str,
        task: asyncio.Task[None],
    ) -> None:
        self._tool_tasks.discard(task)
        self._inflight_tool_calls.discard(tool_call_id)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.warning(
                "Realtime tool delivery failed call_id=%s tool_call_id=%s error_type=%s",
                self.call_id,
                tool_call_id,
                type(error).__name__,
            )
            self._fatal_tool_error = error
            self._tool_connection_error = error
            self._tool_connection_failure.set()
            socket = self._socket
            if socket is not None:
                close_task = asyncio.create_task(
                    socket.close(code=1011, reason="tool delivery failed"),
                    name=f"agent-hotline-close-failed-tool-{tool_call_id}",
                )
                close_task.add_done_callback(_consume_task_exception)

    async def _send_tool_output(
        self,
        socket: RealtimeSocket,
        tool_call_id: str,
        payload: Mapping[str, Any],
        *,
        request_response: bool,
    ) -> None:
        output_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        await self._send_untracked_tool_output_json(
            socket,
            tool_call_id,
            output_json,
            request_response=request_response,
        )

    async def _send_untracked_tool_output_json(
        self,
        socket: RealtimeSocket,
        tool_call_id: str,
        output_json: str,
        *,
        request_response: bool,
    ) -> None:
        await self._send(
            socket,
            {
                "event_id": _tool_event_id(tool_call_id, "untracked-output"),
                "type": "conversation.item.create",
                "item": {
                    "id": _tool_item_id(tool_call_id),
                    "type": "function_call_output",
                    "call_id": tool_call_id,
                    "output": output_json,
                },
            },
        )
        if request_response:
            response: dict[str, Any] = {"type": "response.create"}
            readback = self._pending_readback_from_output(output_json)
            if readback is not None:
                response["response"] = {
                    "instructions": (
                        "Speak the following server-generated readback verbatim and say "
                        "nothing before or after it. Preserve every identifier and qualifier: "
                        + readback
                    )
                }
            await self._send(socket, response)

    async def _reject_parallel_tool_calls(
        self,
        socket: RealtimeSocket,
        tool_calls: list[tuple[str, str, str]],
    ) -> None:
        payload = _error_payload(
            "Call exactly one Agent Hotline tool at a time, then wait for its result.",
            retryable=True,
        )
        output_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        for tool_call_id, _name, _arguments in tool_calls:
            await self._send_untracked_tool_output_json(
                socket,
                tool_call_id,
                output_json,
                request_response=False,
            )
        await self._send(
            socket,
            {
                "event_id": _batch_event_id(tool_calls),
                "type": "response.create",
                "response": {
                    "instructions": (
                        "Explain briefly that you need to handle one operation at a time, "
                        "then continue by calling only the first necessary tool."
                    )
                },
            },
        )

    async def _receive_or_tool_failure(self, socket: RealtimeSocket) -> str | bytes:
        receive = asyncio.create_task(socket.recv())
        failed = asyncio.create_task(self._tool_connection_failure.wait())
        children = (receive, failed)
        try:
            done, _pending = await asyncio.wait(
                children,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failed in done and self._tool_connection_failure.is_set():
                raise self._tool_connection_error or OpenAIRealtimeError(
                    "Realtime tool processing failed"
                )
            return receive.result()
        finally:
            for task in children:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*children, return_exceptions=True)

    def _pending_readback_from_output(self, output_json: str) -> str | None:
        current_verification = getattr(
            self.dispatcher,
            "_current_verification",
            None,
        )
        if not callable(current_verification):
            return None
        window = current_verification()
        if window is None or window.readback_delivered or window.armed:
            return None
        try:
            payload = json.loads(output_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not payload.get("ok"):
            return None
        candidate = payload.get("response_text") or payload.get("exact_readback")
        if not isinstance(candidate, str):
            return None
        if not _spoken_text_matches(window.expected_readback, candidate):
            return None
        return window.expected_readback

    async def _send_trusted_signal(
        self,
        socket: RealtimeSocket,
        status: Mapping[str, Any],
    ) -> None:
        await self._send(
            socket,
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": str(status["message"]),
                        }
                    ],
                },
            },
        )
        await self._send(socket, {"type": "response.create"})

    async def _send(self, socket: RealtimeSocket, event: Mapping[str, Any]) -> None:
        payload = json.dumps(
            event,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        async with self._send_lock:
            await socket.send(payload)


class OpenAIRealtimeManager:
    """Daemon-owned registry for verified SIP admission and active conversations."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        coordinator: HotlineCoordinator,
        client: OpenAIRealtimeClient | None = None,
        socket_factory: SocketFactory | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.coordinator = coordinator
        self.client = client or OpenAIRealtimeClient(settings)
        self.socket_factory = socket_factory or _default_socket_factory
        self._owns_client = client is None
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._conversations: dict[str, RealtimeConversation] = {}
        self._call_locks: dict[str, asyncio.Lock] = {}
        self._admission_lock = asyncio.Lock()
        self._termination_reconciliation_lock = asyncio.Lock()
        self._closed = False
        self.coordinator.bind_call_termination_scheduler(
            self.schedule_call_termination,
        )

    @property
    def active_calls(self) -> int:
        return sum(not task.done() for task in self._workers.values())

    async def schedule_call_termination(self, session: ContactSession) -> None:
        """Enqueue and immediately attempt every provider leg known to a session."""

        current = await self.store.get_session(session.session_id)
        if current is None:
            raise OpenAIRealtimeError("Call termination session no longer exists")
        await self._terminate_call_paths(
            current.interaction_id,
            attempt_id=current.attempt_id,
            session_hint=current,
        )

    async def handle_webhook(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> IncomingCallResult:
        if self._closed:
            raise OpenAIRealtimeError("Realtime manager is closed")
        event = self.client.unwrap_webhook(raw_body, headers)
        if event.get("type") != "realtime.call.incoming":
            return IncomingCallResult(handled=False, accepted=False, duplicate=False)
        incoming = _parse_incoming_call(event)
        webhook_id = _header_value(headers, "webhook-id") or incoming.event_id
        receipt_key = f"openai:{webhook_id}"
        fingerprint = hashlib.sha256(raw_body).hexdigest()
        lock = self._call_locks.setdefault(incoming.call_id, asyncio.Lock())
        async with lock:
            claim = await self.store.claim_ingress_receipt(
                receipt_key,
                kind="openai.realtime.call.incoming",
                subject_id=incoming.call_id,
                fingerprint=fingerprint,
            )
            if claim.processed:
                active = await self._resolve_duplicate_call(incoming.call_id)
                return IncomingCallResult(
                    handled=True,
                    accepted=active is not None,
                    duplicate=True,
                    call_id=incoming.call_id,
                    event_id=active.event_id if active is not None else None,
                )
            if incoming.call_id in self._workers:
                await self.store.complete_ingress_receipt(receipt_key)
                conversation = self._conversations[incoming.call_id]
                return IncomingCallResult(
                    handled=True,
                    accepted=True,
                    duplicate=True,
                    call_id=incoming.call_id,
                    event_id=conversation.event_id,
                )
            try:
                async with self._admission_lock:
                    if self.active_calls >= self.settings.hotline_max_active_calls:
                        raise IncomingCallError(
                            "the owner already has the maximum number of active calls",
                            sip_status=486,
                        )
                    result = await self._admit_call(incoming)
            except IncomingCallError as exc:
                try:
                    await self.client.reject_call(incoming.call_id, status_code=exc.sip_status)
                except Exception:
                    await self.store.release_ingress_receipt(receipt_key)
                    raise
                await self.store.complete_ingress_receipt(receipt_key)
                return IncomingCallResult(
                    handled=True,
                    accepted=False,
                    duplicate=not claim.created,
                    call_id=incoming.call_id,
                    reason=str(exc),
                )
            except Exception:
                await self.store.release_ingress_receipt(receipt_key)
                raise
            await self.store.complete_ingress_receipt(receipt_key)
            return IncomingCallResult(
                handled=True,
                accepted=True,
                duplicate=not claim.created,
                call_id=incoming.call_id,
                event_id=result.event_id,
            )

    async def recover_active_calls(self) -> int:
        """Fail closed active calls whose sideband event stream was interrupted."""

        if self._closed:
            raise OpenAIRealtimeError("Realtime manager is closed")
        await self.reconcile_pending_call_terminations()
        await self.revoke_unconsumed_call_admissions()
        await self.recover_orphaned_call_legs()
        terminated = await self._drain_recoverable_realtime_sessions(
            reason="daemon restarted without a replayable Realtime event cursor",
        )
        if terminated:
            logger.warning(
                "Terminated %s active Realtime call(s) after a sideband continuity gap",
                terminated,
            )
        await self.reconcile_pending_call_terminations()
        return terminated

    async def _drain_recoverable_realtime_sessions(
        self,
        *,
        reason: str,
    ) -> int:
        """Fail close all persisted active sessions, independent of live capacity."""

        terminated = 0
        while True:
            sessions = await self.store.list_recoverable_realtime_sessions(
                limit=_TERMINATION_RECONCILE_BATCH,
            )
            if not sessions:
                break
            progressed = 0
            async with self._admission_lock:
                for session in sessions:
                    if session.interaction_id in self._workers:
                        continue
                    await self._fail_closed_session(
                        session,
                        reason=reason,
                    )
                    terminated += 1
                    progressed += 1
            if progressed == 0:
                break
        return terminated

    async def recover_orphaned_call_legs(self) -> int:
        """Durably adopt every consumed carrier admission missing its session."""

        recovered = 0
        while True:
            orphaned_legs = await self.store.list_orphaned_consumed_carrier_legs(
                limit=_TERMINATION_RECONCILE_BATCH,
            )
            if not orphaned_legs:
                return recovered
            for leg in orphaned_legs:
                await self._terminate_call_paths(
                    leg.provider_call_id,
                    attempt_id=leg.call_sid,
                )
                recovered += 1

    async def revoke_unconsumed_call_admissions(self) -> int:
        """Revoke pre-SIP carrier parents and enqueue them atomically."""

        revoked = 0
        while True:
            jobs = await self.store.revoke_unconsumed_carrier_admissions(
                limit=_TERMINATION_RECONCILE_BATCH,
            )
            if not jobs:
                return revoked
            revoked += len(jobs)
            await self.reconcile_pending_call_terminations(
                job_ids=(job.job_id for job in jobs),
            )

    async def expire_overdue_calls(self) -> int:
        """Fail closed calls that outlive the configured owner-channel lease."""

        sessions = await self.store.list_overdue_realtime_sessions(
            max_age_seconds=self.settings.hotline_max_call_duration_seconds,
            limit=max(self.settings.hotline_max_active_calls, 20),
        )
        expired = 0
        for session in sessions:
            await self._fail_closed_session(
                session,
                reason="maximum call duration elapsed",
            )
            if session.interaction_id is not None:
                await self.stop_call(session.interaction_id, hangup=False)
            expired += 1
        if expired:
            logger.warning(
                "Expired %s Realtime call(s) after the maximum call duration",
                expired,
            )
        return expired

    async def bind_outbound_carrier_parent(
        self,
        *,
        event_id: str,
        call_sid: str,
    ) -> tuple[ContactSession, bool]:
        """Atomically bind Twilio's allocated parent before returning outbound TwiML."""

        if _TWILIO_CALL_SID_RE.fullmatch(call_sid) is None:
            raise IncomingCallError("carrier parent CallSid is invalid", sip_status=481)
        event = await self.store.require_event(event_id)
        sessions = [
            candidate
            for candidate in await self.store.list_sessions(
                event_id=event_id,
                limit=5,
            )
            if candidate.direction is ContactDirection.OUTBOUND_ESCALATION
        ]
        exact = [
            candidate
            for candidate in sessions
            if candidate.attempt_id is not None
            and hmac.compare_digest(candidate.attempt_id, call_sid)
        ]
        if len(exact) == 1:
            session = exact[0]
        else:
            unbound = [candidate for candidate in sessions if candidate.attempt_id is None]
            if exact or len(unbound) != 1:
                raise IncomingCallError(
                    "carrier parent has no unique outbound session",
                    sip_status=481,
                )
            try:
                session = await self.store.link_attempt(
                    unbound[0].session_id,
                    call_sid,
                )
            except ConflictError:
                session = await self.store.get_session_by_attempt(call_sid)
                if session is None or session.event_id != event_id:
                    raise IncomingCallError(
                        "carrier parent could not be linked",
                        sip_status=481,
                    ) from None

        event = await self.store.require_event(event_id)
        should_dial = (
            event.state not in TERMINAL_EVENT_STATES
            and session.state not in TERMINAL_SESSION_STATES
        )
        if not should_dial:
            await self._terminate_call_paths(
                session.interaction_id,
                attempt_id=call_sid,
                session_hint=session,
            )
            if session.state not in TERMINAL_SESSION_STATES:
                with contextlib.suppress(Exception):
                    session = await self.store.transition_session(
                        session.session_id,
                        SessionState.CANCELLED,
                        failure_reason="carrier parent arrived after event termination",
                    )
            if event.state not in TERMINAL_EVENT_STATES:
                with contextlib.suppress(Exception):
                    await self.store.transition_event(
                        event.event_id,
                        EventState.FAILED,
                        details={"reason": "outbound_session_already_terminal"},
                    )
        return session, should_dial

    async def handle_carrier_status(
        self,
        *,
        call_sid: str,
        status: str,
        receipt_id: str | None = None,
        correlated_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile a verified Twilio status callback without trusting it as authority."""

        async with self._admission_lock:
            return await self._handle_carrier_status_locked(
                call_sid=call_sid,
                status=status,
                receipt_id=receipt_id,
                correlated_event_id=correlated_event_id,
            )

    async def _handle_carrier_status_locked(
        self,
        *,
        call_sid: str,
        status: str,
        receipt_id: str | None,
        correlated_event_id: str | None,
    ) -> dict[str, Any]:
        session = await self.store.get_session_by_attempt(call_sid)
        if session is None and correlated_event_id is not None:
            session, _should_dial = await self.bind_outbound_carrier_parent(
                event_id=correlated_event_id,
                call_sid=call_sid,
            )
        if session is None or session.event_id is None:
            raise IncomingCallError("carrier call is not linked to an event", sip_status=481)
        if correlated_event_id is not None and not hmac.compare_digest(
            session.event_id, correlated_event_id
        ):
            raise IncomingCallError(
                "carrier callback event does not match its session",
                sip_status=481,
            )
        event = await self.store.require_event(session.event_id)
        if event.state in TERMINAL_EVENT_STATES:
            await self._terminate_call_paths(
                session.interaction_id,
                attempt_id=call_sid,
            )
            if session.interaction_id is not None:
                await self.stop_call(session.interaction_id, hangup=False)
            if session.state not in TERMINAL_SESSION_STATES:
                with contextlib.suppress(Exception):
                    await self.store.transition_session(
                        session.session_id,
                        SessionState.CANCELLED,
                        failure_reason="carrier callback arrived after event termination",
                    )
            return {
                "accepted": True,
                "terminal": True,
                "event_id": session.event_id,
                "termination": "scheduled",
            }
        normalized = status.strip().lower().replace("_", "-")
        if normalized in {"queued", "initiated"}:
            return {"accepted": True, "terminal": False}
        if normalized == "ringing":
            if session.state is SessionState.DIALING:
                await self.store.transition_session(session.session_id, SessionState.RINGING)
            return {"accepted": True, "terminal": False}
        if normalized in {"in-progress", "answered"}:
            current = await self.store.get_session(session.session_id)
            if current is not None and current.state in {
                SessionState.DIALING,
                SessionState.RINGING,
            }:
                await self.store.transition_session(current.session_id, SessionState.CONNECTED)
            event = await self.store.require_event(session.event_id)
            if event.state is EventState.DIALING:
                await self.store.transition_event(event.event_id, EventState.CONNECTED)
            return {"accepted": True, "terminal": False}

        status_map = {
            "completed": WebhookStatus.COMPLETED,
            "busy": WebhookStatus.BUSY,
            "no-answer": WebhookStatus.NO_ANSWER,
            "failed": WebhookStatus.FAILED,
            "canceled": WebhookStatus.FAILED,
        }
        try:
            terminal = status_map[normalized]
        except KeyError as exc:
            raise ValueError("unsupported carrier call status") from exc
        interaction_id = session.interaction_id
        payload = ProviderWebhookPayload(
            webhook_id=(
                f"twilio:{receipt_id}"
                if receipt_id
                else "twilio:"
                + hashlib.sha256(f"{call_sid}:{normalized}".encode()).hexdigest()[:48]
            ),
            attempt_id=call_sid,
            interaction_id=interaction_id,
            status=terminal,
            provider="twilio_openai_realtime",
            failure_reason=(
                "carrier reported call failure" if terminal is WebhookStatus.FAILED else None
            ),
            metadata={"source": "twilio_status_callback"},
        )
        await self._terminate_call_paths(
            interaction_id,
            attempt_id=call_sid,
            session_hint=session,
        )
        result = await self.coordinator.reconcile_provider_completion(payload)
        if interaction_id is not None:
            await self.stop_call(interaction_id, hangup=False)
        return result

    async def stop_call(self, call_id: str, *, hangup: bool = True) -> None:
        conversation = self._conversations.get(call_id)
        if hangup:
            session = await self.store.get_session_by_interaction(call_id)
            attempt_id = (
                conversation.attempt_id
                if conversation is not None
                else session.attempt_id
                if session is not None
                else None
            )
            await self._terminate_call_paths(call_id, attempt_id=attempt_id)
        if conversation is not None:
            await conversation.stop()
        task = self._workers.get(call_id)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _admit_call(self, incoming: ParsedIncomingCall) -> RealtimeConversation:
        correlation = _parse_correlation(
            incoming.sip_headers,
            self.settings.hotline_sip_correlation_secret.get_secret_value(),
        )
        if correlation is None:
            raise IncomingCallError(
                "verified carrier correlation is required",
                sip_status=403,
            )
        session: Any | None = None
        attempt_id: str | None = correlation.call_sid
        cleanup_required = _TWILIO_CALL_SID_RE.fullmatch(correlation.call_sid) is not None
        accepted = False
        if not cleanup_required:
            raise IncomingCallError(
                "carrier parent CallSid is invalid",
                sip_status=403,
            )
        try:
            _validate_twilio_sip_identity(
                incoming.sip_headers,
                expected_account_sid=self.settings.twilio_account_sid or "",
            )
            if correlation.direction == "outbound":
                assert correlation.event_id is not None
                # Both provider legs are now known. Establish cleanup
                # correlation before the first await so cancellation during
                # durable lookup cannot strand the accepted OpenAI leg or its
                # carrier parent.
                sessions = await self.store.list_sessions(
                    event_id=correlation.event_id,
                    limit=5,
                )
                session = next(
                    (
                        candidate
                        for candidate in sessions
                        if candidate.direction is ContactDirection.OUTBOUND_ESCALATION
                        and candidate.state
                        in {
                            SessionState.DIALING,
                            SessionState.RINGING,
                            SessionState.CONNECTED,
                            SessionState.DISCUSSING,
                        }
                    ),
                    None,
                )
                if session is None:
                    raise IncomingCallError(
                        "outbound call correlation was not found",
                        sip_status=481,
                    )
                if session.attempt_id is None or not hmac.compare_digest(
                    session.attempt_id,
                    attempt_id,
                ):
                    raise IncomingCallError(
                        "outbound parent CallSid does not match",
                        sip_status=481,
                    )
                event = await self.store.require_event(correlation.event_id)
                if event.state not in {
                    EventState.DIALING,
                    EventState.CONNECTED,
                    EventState.AWAITING_DECISION,
                }:
                    raise IncomingCallError("outbound event is no longer active", sip_status=603)
                if session.interaction_id is None:
                    session = await self.store.link_interaction(
                        session.session_id,
                        incoming.call_id,
                        require_active=True,
                    )
                elif session.interaction_id != incoming.call_id:
                    raise IncomingCallError("outbound interaction does not match", sip_status=481)
                direction: Direction = "outbound_escalation"
            else:
                if (
                    correlation.caller_phone is None
                    or correlation.admission_nonce is None
                    or correlation.expires_at_epoch is None
                ):
                    raise IncomingCallError("verified carrier caller is missing", sip_status=403)
                try:
                    await self.store.consume_carrier_admission(
                        correlation.call_sid,
                        caller_phone=correlation.caller_phone,
                        admission_nonce=correlation.admission_nonce,
                        expires_at_epoch=correlation.expires_at_epoch,
                        provider_call_id=incoming.call_id,
                    )
                except StorageError as exc:
                    raise IncomingCallError(
                        "carrier admission is expired, consumed, or unknown",
                        sip_status=403,
                    ) from exc
                try:
                    admission = await self.coordinator.begin_inbound_session(
                        BeginInboundSessionRequest(
                            caller_phone_number=correlation.caller_phone,
                            interaction_id=incoming.call_id,
                        ),
                        provider="openai_realtime",
                    )
                except ActiveSessionError as exc:
                    raise IncomingCallError(
                        "another owner call is already active",
                        sip_status=486,
                    ) from exc
                if not admission.accepted or admission.event_id is None:
                    raise IncomingCallError(admission.message_to_user, sip_status=403)
                event = await self.store.require_event(admission.event_id)
                session = await self.store.get_session_by_interaction(incoming.call_id)
                if session is None:
                    raise OpenAIRealtimeError("inbound session was not persisted")
                if session.state not in {
                    SessionState.PENDING,
                    SessionState.DIALING,
                    SessionState.RINGING,
                    SessionState.CONNECTED,
                    SessionState.DISCUSSING,
                }:
                    raise IncomingCallError(
                        "inbound call correlation is no longer active",
                        sip_status=603,
                    )
                if session.attempt_id is None:
                    session = await self.store.link_attempt(
                        session.session_id,
                        attempt_id,
                        require_active=True,
                    )
                direction = "inbound_control"

            session_config = build_realtime_session_config(
                self.settings,
                direction=direction,
            )
            await self.client.accept_call(incoming.call_id, session_config)
            accepted = True
            current_session = await self.store.get_session(session.session_id)
            if current_session is not None and current_session.state in {
                SessionState.DIALING,
                SessionState.RINGING,
            }:
                await self.store.transition_session(
                    current_session.session_id,
                    SessionState.CONNECTED,
                )
            current_event = await self.store.require_event(event.event_id)
            if current_event.state is EventState.DIALING:
                current_event = await self.store.transition_event(
                    current_event.event_id,
                    EventState.CONNECTED,
                )
            if (
                direction == "outbound_escalation"
                and current_event.blocking
                and current_event.state is EventState.CONNECTED
            ):
                await self.store.transition_event(
                    current_event.event_id,
                    EventState.AWAITING_DECISION,
                )
            session = await self.store.get_session(session.session_id) or session
            current_event = await self.store.require_event(event.event_id)
            if (
                session.state in TERMINAL_SESSION_STATES
                or current_event.state in TERMINAL_EVENT_STATES
                or (
                    current_event.deadline_at is not None and utc_now() >= current_event.deadline_at
                )
            ):
                raise IncomingCallError(
                    "call correlation became terminal during admission",
                    sip_status=603,
                )
            if session.attempt_id is None:
                raise OpenAIRealtimeError("accepted call has no carrier correlation")
            return self._start_conversation(
                call_id=incoming.call_id,
                attempt_id=session.attempt_id,
                event_id=event.event_id,
                session_id=session.session_id,
                direction=direction,
            )
        except BaseException as exc:
            if cleanup_required:
                cleanup_task = asyncio.create_task(
                    self._cleanup_failed_admission(
                        incoming.call_id,
                        session=session,
                        attempt_id=attempt_id,
                        accepted=accepted,
                    ),
                    name=f"agent-hotline-admission-cleanup-{incoming.call_id}",
                )
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    with contextlib.suppress(Exception):
                        await cleanup_task
                except Exception as cleanup_exc:
                    logger.error(
                        "Realtime admission cleanup failed call_id=%s error_type=%s",
                        incoming.call_id,
                        type(cleanup_exc).__name__,
                    )
            if isinstance(exc, IncomingCallError) or not isinstance(exc, Exception):
                raise
            raise OpenAIRealtimeError("OpenAI Realtime call admission failed closed") from exc

    async def _cleanup_failed_admission(
        self,
        call_id: str,
        *,
        session: Any | None,
        attempt_id: str | None,
        accepted: bool,
    ) -> None:
        """Terminate and durably reconcile every partially admitted call."""

        known_session = session
        if known_session is None:
            with contextlib.suppress(Exception):
                known_session = await self.store.get_session_by_interaction(call_id)
        if attempt_id is None and known_session is not None:
            attempt_id = known_session.attempt_id

        conversation = self._conversations.pop(call_id, None)
        if conversation is not None:
            with contextlib.suppress(Exception):
                await conversation.stop()
        worker = self._workers.pop(call_id, None)
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

        await self._terminate_call_paths(call_id, attempt_id=attempt_id)
        if known_session is None or known_session.event_id is None or attempt_id is None:
            return
        failure_payload = ProviderWebhookPayload(
            webhook_id=(
                "openai:" + hashlib.sha256(f"{call_id}:admission-failed".encode()).hexdigest()[:48]
            ),
            attempt_id=attempt_id,
            interaction_id=call_id,
            status=WebhookStatus.FAILED,
            provider="openai_realtime",
            failure_reason="OpenAI Realtime call admission failed closed",
            metadata={
                "source": "openai_realtime_admission",
                "accepted_before_failure": accepted,
            },
        )
        with contextlib.suppress(Exception):
            await self.coordinator.reconcile_provider_completion(failure_payload)

    async def _resolve_duplicate_call(self, call_id: str) -> RealtimeConversation | None:
        """Return a live duplicate or fail closed when sideband continuity was lost."""

        existing = self._conversations.get(call_id)
        if existing is not None and call_id in self._workers:
            return existing
        session = await self.store.get_session_by_interaction(call_id)
        if session is None:
            return None
        async with self._admission_lock:
            existing = self._conversations.get(call_id)
            if existing is not None and call_id in self._workers:
                return existing
            await self._fail_closed_session(
                session,
                reason="Realtime webhook replay arrived after sideband continuity was lost",
            )
            return None

    async def _fail_closed_session(
        self,
        session: Any,
        *,
        reason: str,
    ) -> None:
        if session.provider != "openai_realtime":
            return
        attempt_id = session.attempt_id
        if attempt_id is None and session.interaction_id is not None:
            consumed_leg = await self.store.get_consumed_carrier_leg(
                session.interaction_id,
            )
            if consumed_leg is not None:
                attempt_id = consumed_leg.call_sid
                with contextlib.suppress(Exception):
                    session = await self.store.link_attempt(
                        session.session_id,
                        attempt_id,
                    )
        if attempt_id is None and session.interaction_id is None:
            with contextlib.suppress(Exception):
                await self.store.transition_session(
                    session.session_id,
                    SessionState.FAILED,
                    failure_reason=reason,
                )
            if session.event_id is not None:
                with contextlib.suppress(Exception):
                    await self.store.transition_event(
                        session.event_id,
                        EventState.FAILED,
                        details={"reason": "realtime_sideband_continuity_lost"},
                    )
            return
        await self._terminate_call_paths(
            session.interaction_id,
            attempt_id=attempt_id,
        )
        if attempt_id is not None:
            correlation_id = session.interaction_id or attempt_id
            payload = ProviderWebhookPayload(
                webhook_id=(
                    "openai:"
                    + hashlib.sha256(f"{correlation_id}:sideband-gap".encode()).hexdigest()[:48]
                ),
                attempt_id=attempt_id,
                interaction_id=session.interaction_id,
                status=WebhookStatus.FAILED,
                provider="openai_realtime",
                failure_reason=reason,
                metadata={"source": "openai_realtime_sideband_gap"},
            )
            with contextlib.suppress(Exception):
                await self.coordinator.reconcile_provider_completion(payload)
        latest_session = await self.store.get_session(session.session_id)
        if latest_session is not None and latest_session.state not in {
            SessionState.COMPLETED,
            SessionState.NO_ANSWER,
            SessionState.BUSY,
            SessionState.FAILED,
            SessionState.CANCELLED,
        }:
            with contextlib.suppress(Exception):
                await self.store.transition_session(
                    latest_session.session_id,
                    SessionState.FAILED,
                    failure_reason=reason,
                )
        if session.event_id is not None:
            latest_event = await self.store.get_event(session.event_id)
            if latest_event is not None and latest_event.state in {
                EventState.DETECTED,
                EventState.QUEUED,
                EventState.DIALING,
                EventState.CONNECTED,
                EventState.AWAITING_DECISION,
            }:
                with contextlib.suppress(Exception):
                    await self.store.transition_event(
                        latest_event.event_id,
                        EventState.FAILED,
                        details={"reason": "realtime_sideband_continuity_lost"},
                    )

    async def _terminate_call_paths(
        self,
        call_id: str | None,
        *,
        attempt_id: str | None,
        session_hint: ContactSession | None = None,
    ) -> None:
        """Durably enqueue both provider legs, then attempt any newly due work."""

        session = session_hint
        try:
            if session is None and call_id is not None:
                session = await self.store.get_session_by_interaction(call_id)
            if session is None and attempt_id is not None:
                session = await self.store.get_session_by_attempt(attempt_id)
        except Exception as exc:
            logger.warning(
                "Call termination could not load durable session error_type=%s",
                type(exc).__name__,
            )
        try:
            jobs = await self.store.ensure_call_termination_jobs(
                openai_call_id=call_id,
                carrier_call_sid=attempt_id,
                session_id=session.session_id if session is not None else None,
                event_id=session.event_id if session is not None else None,
            )
        except Exception as exc:
            logger.error(
                "Call termination could not be durably enqueued error_type=%s",
                type(exc).__name__,
            )
            await self._best_effort_immediate_termination(
                call_id=call_id,
                attempt_id=attempt_id,
            )
            raise OpenAIRealtimeError("Call termination could not be durably enqueued") from exc
        if session is not None:
            with contextlib.suppress(Exception):
                await self.store.append_timeline(
                    TimelineEntry(
                        event_id=session.event_id,
                        session_id=session.session_id,
                        kind=TimelineKind.CALL_TERMINATION_REQUESTED,
                        details={
                            "legs": [job.leg.value for job in jobs],
                        },
                    )
                )
        await self.reconcile_pending_call_terminations(
            job_ids=(job.job_id for job in jobs),
        )

    async def _best_effort_immediate_termination(
        self,
        *,
        call_id: str | None,
        attempt_id: str | None,
    ) -> None:
        """Attempt known provider legs without treating volatile I/O as scheduled."""

        if call_id is not None:
            try:
                await self.client.hangup_call(call_id)
            except Exception as exc:
                logger.warning(
                    "Immediate OpenAI termination after queue failure was unknown error_type=%s",
                    type(exc).__name__,
                )
        if attempt_id is not None:
            terminate_carrier = getattr(self.coordinator.provider, "terminate_call", None)
            if not callable(terminate_carrier):
                logger.warning("Immediate carrier termination after queue failure is unavailable")
                return
            try:
                await terminate_carrier(attempt_id)
            except Exception as exc:
                logger.warning(
                    "Immediate carrier termination after queue failure was unknown error_type=%s",
                    type(exc).__name__,
                )

    async def reconcile_pending_call_terminations(
        self,
        *,
        job_ids: Iterable[str] | None = None,
    ) -> int:
        """Attempt due termination jobs without dropping unknown outcomes.

        Each claim advances a bounded retry deadline before provider I/O. A
        crash or ambiguous provider error therefore leaves durable pending work
        for the next maintenance pass or daemon start.
        """

        async with self._termination_reconciliation_lock:
            if job_ids is None:
                candidates = await self.store.list_due_call_termination_jobs(
                    limit=_TERMINATION_RECONCILE_BATCH,
                )
            else:
                candidates = []
                for job_id in dict.fromkeys(job_ids):
                    job = await self.store.get_call_termination_job(job_id)
                    if job is not None:
                        candidates.append(job)
            attempted = 0
            for candidate in candidates:
                if candidate.state is CallTerminationState.CONFIRMED:
                    continue
                claimed = await self.store.claim_call_termination_attempt(
                    candidate.job_id,
                    base_delay_seconds=_TERMINATION_RETRY_BASE_SECONDS,
                    max_delay_seconds=_TERMINATION_RETRY_MAX_SECONDS,
                )
                if claimed is None:
                    continue
                attempted += 1
                try:
                    await self._attempt_call_termination(claimed)
                except Exception as exc:
                    error_type = type(exc).__name__
                    try:
                        failed = await self.store.record_call_termination_failure(
                            claimed.job_id,
                            attempt_number=claimed.attempts,
                            error_type=error_type,
                        )
                    except Exception as persistence_exc:
                        logger.error(
                            "Call termination failure metadata could not be persisted "
                            "leg=%s error_type=%s",
                            claimed.leg.value,
                            type(persistence_exc).__name__,
                        )
                    else:
                        with contextlib.suppress(Exception):
                            await self.store.append_timeline(
                                TimelineEntry(
                                    event_id=failed.event_id,
                                    session_id=failed.session_id,
                                    kind=TimelineKind.CALL_TERMINATION_UNKNOWN,
                                    details={
                                        "leg": failed.leg.value,
                                        "attempt": failed.attempts,
                                        "error_type": error_type,
                                        "next_attempt_at": failed.next_attempt_at.isoformat(),
                                    },
                                )
                            )
                    logger.warning(
                        "Call termination remains pending leg=%s attempt=%s error_type=%s",
                        claimed.leg.value,
                        claimed.attempts,
                        error_type,
                    )
                    continue
                try:
                    confirmed = await self.store.confirm_call_termination(
                        claimed.job_id,
                        attempt_number=claimed.attempts,
                    )
                except Exception as exc:
                    logger.error(
                        "Call termination succeeded but confirmation persistence failed "
                        "leg=%s error_type=%s",
                        claimed.leg.value,
                        type(exc).__name__,
                    )
                    continue
                with contextlib.suppress(Exception):
                    await self.store.append_timeline(
                        TimelineEntry(
                            event_id=confirmed.event_id,
                            session_id=confirmed.session_id,
                            kind=TimelineKind.CALL_TERMINATION_CONFIRMED,
                            details={
                                "leg": confirmed.leg.value,
                                "attempt": confirmed.attempts,
                            },
                        )
                    )
            return attempted

    async def _attempt_call_termination(self, job: CallTerminationJob) -> None:
        if job.leg is CallTerminationLeg.OPENAI:
            await self.client.hangup_call(job.target_id)
            return
        if job.leg is CallTerminationLeg.CARRIER:
            terminate_carrier = getattr(self.coordinator.provider, "terminate_call", None)
            if not callable(terminate_carrier):
                raise OpenAIRealtimeError("carrier termination is unavailable")
            await terminate_carrier(job.target_id)
            return
        raise OpenAIRealtimeError("unsupported durable termination leg")

    def _start_conversation(
        self,
        *,
        call_id: str,
        attempt_id: str,
        event_id: str,
        session_id: str,
        direction: Direction,
    ) -> RealtimeConversation:
        conversation = RealtimeConversation(
            settings=self.settings,
            store=self.store,
            client=self.client,
            socket_factory=self.socket_factory,
            call_id=call_id,
            attempt_id=attempt_id,
            event_id=event_id,
            session_id=session_id,
            direction=direction,
            coordinator=self.coordinator,
        )
        self._conversations[call_id] = conversation
        task = asyncio.create_task(
            self._run_conversation(conversation),
            name=f"agent-hotline-realtime-call-{call_id}",
        )
        self._workers[call_id] = task
        return conversation

    async def _run_conversation(self, conversation: RealtimeConversation) -> None:
        failure: Exception | None = None
        try:
            async with asyncio.timeout(
                self.settings.hotline_max_call_duration_seconds,
            ):
                await conversation.run()
        except asyncio.CancelledError:
            failure = OpenAIRealtimeError("Realtime conversation worker was cancelled")
            raise
        except TimeoutError:
            failure = OpenAIRealtimeError(
                "Realtime conversation exceeded the maximum call duration"
            )
            logger.warning(
                "Realtime conversation reached its duration limit call_id=%s",
                conversation.call_id,
            )
        except Exception as exc:
            failure = exc
            logger.warning(
                "Realtime conversation failed call_id=%s error_type=%s",
                conversation.call_id,
                type(exc).__name__,
            )
        finally:
            self._workers.pop(conversation.call_id, None)
            self._conversations.pop(conversation.call_id, None)
            if failure is None and conversation._stopped_externally:
                failure = OpenAIRealtimeError("Realtime conversation was stopped externally")
            if not conversation._stopped_externally:
                await self._terminate_call_paths(
                    conversation.call_id,
                    attempt_id=conversation.attempt_id,
                )
            session = await self.store.get_session(conversation.session_id)
            if session is not None and session.state not in {
                SessionState.COMPLETED,
                SessionState.NO_ANSWER,
                SessionState.BUSY,
                SessionState.FAILED,
                SessionState.CANCELLED,
            }:
                payload = ProviderWebhookPayload(
                    webhook_id=(
                        "openai:"
                        + hashlib.sha256(
                            (
                                f"{conversation.call_id}:{'failed' if failure else 'completed'}"
                            ).encode()
                        ).hexdigest()[:48]
                    ),
                    attempt_id=conversation.attempt_id,
                    interaction_id=conversation.call_id,
                    status=(WebhookStatus.FAILED if failure else WebhookStatus.COMPLETED),
                    provider="openai_realtime",
                    failure_reason=("Realtime sideband ended unexpectedly" if failure else None),
                    metadata={"source": "openai_realtime_sideband"},
                )
                with contextlib.suppress(Exception):
                    await self.coordinator.reconcile_provider_completion(payload)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        call_ids = tuple(self._conversations)
        await asyncio.gather(
            *(self.stop_call(call_id, hangup=True) for call_id in call_ids),
            return_exceptions=True,
        )
        tasks = tuple(self._workers.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.revoke_unconsumed_call_admissions()
            await self.recover_orphaned_call_legs()
            await self._drain_recoverable_realtime_sessions(
                reason="Realtime manager shut down before provider completion",
            )
            await self.reconcile_pending_call_terminations()
        except Exception as exc:
            # Durable active session/admission state is deliberately retained,
            # so the next process can retry even when shutdown persistence is
            # unavailable.
            logger.error(
                "Realtime shutdown call drain failed error_type=%s",
                type(exc).__name__,
            )
        finally:
            if self._owns_client:
                await self.client.close()


def _parse_incoming_call(event: Mapping[str, Any]) -> ParsedIncomingCall:
    event_id = event.get("id")
    data = event.get("data")
    if not isinstance(event_id, str) or not isinstance(data, Mapping):
        raise IncomingCallError("incoming webhook is malformed", sip_status=400)
    call_id = data.get("call_id")
    if not isinstance(call_id, str) or _CALL_ID_RE.fullmatch(call_id) is None:
        raise IncomingCallError("incoming call ID is invalid", sip_status=400)
    raw_headers = data.get("sip_headers")
    if not isinstance(raw_headers, list):
        raise IncomingCallError("incoming SIP headers are missing", sip_status=400)
    headers: dict[str, str] = {}
    for item in raw_headers:
        if not isinstance(item, Mapping):
            raise IncomingCallError("incoming SIP header is malformed", sip_status=400)
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            raise IncomingCallError("incoming SIP header is malformed", sip_status=400)
        normalized = name.strip().lower()
        if not normalized or len(normalized) > 100 or len(value) > 2000:
            raise IncomingCallError("incoming SIP header is invalid", sip_status=400)
        existing = headers.get(normalized)
        if existing is not None and not hmac.compare_digest(existing, value):
            raise IncomingCallError("conflicting duplicate SIP header", sip_status=400)
        headers[normalized] = value.strip()
    return ParsedIncomingCall(
        event_id=event_id,
        call_id=call_id,
        sip_headers=headers,
    )


def _validate_twilio_sip_identity(
    headers: Mapping[str, str],
    *,
    expected_account_sid: str,
) -> None:
    """Validate Twilio's child SIP identity without confusing it for the parent."""

    call_sid = headers.get("x-twilio-callsid")
    account_sid = headers.get("x-twilio-accountsid")
    if (
        call_sid is None
        or account_sid is None
        or _TWILIO_CALL_SID_RE.fullmatch(call_sid) is None
        or _TWILIO_ACCOUNT_SID_RE.fullmatch(account_sid) is None
        or _TWILIO_ACCOUNT_SID_RE.fullmatch(expected_account_sid) is None
        or not hmac.compare_digest(account_sid, expected_account_sid)
    ):
        raise IncomingCallError(
            "verified Twilio SIP identity is required",
            sip_status=403,
        )


def _parse_correlation(
    sip_headers: Mapping[str, str],
    secret: str,
) -> SIPCorrelation | None:
    names = {
        "x-hotline-direction",
        "x-hotline-call-sid",
        "x-hotline-event-id",
        "x-hotline-caller",
        "x-hotline-admission",
        "x-hotline-expires",
        "x-hotline-signature",
    }
    present = names.intersection(sip_headers)
    if not present:
        return None
    direction = sip_headers.get("x-hotline-direction")
    call_sid = sip_headers.get("x-hotline-call-sid")
    event_id = sip_headers.get("x-hotline-event-id")
    caller_phone = sip_headers.get("x-hotline-caller")
    admission_nonce = sip_headers.get("x-hotline-admission")
    expires_at_raw = sip_headers.get("x-hotline-expires")
    signature = sip_headers.get("x-hotline-signature")
    try:
        expires_at_epoch = int(expires_at_raw) if expires_at_raw is not None else None
    except ValueError as exc:
        raise IncomingCallError("SIP correlation expiry is invalid", sip_status=403) from exc
    if (
        direction not in {"inbound", "outbound"}
        or call_sid is None
        or signature is None
        or (
            direction == "outbound"
            and (
                event_id is None
                or caller_phone is not None
                or admission_nonce is not None
                or expires_at_epoch is not None
            )
        )
        or (
            direction == "inbound"
            and (
                event_id is not None
                or caller_phone is None
                or admission_nonce is None
                or expires_at_epoch is None
            )
        )
    ):
        raise IncomingCallError("SIP correlation headers are incomplete", sip_status=403)
    if not verify_correlation_signature(
        secret,
        direction=direction,
        call_sid=call_sid,
        event_id=event_id,
        caller_phone=caller_phone,
        admission_nonce=admission_nonce,
        expires_at_epoch=expires_at_epoch,
        signature=signature,
    ):
        raise IncomingCallError("SIP correlation signature is invalid", sip_status=403)
    return SIPCorrelation(
        direction=direction,
        call_sid=call_sid,
        event_id=event_id,
        caller_phone=caller_phone,
        admission_nonce=admission_nonce,
        expires_at_epoch=expires_at_epoch,
    )


def _tool_item_id(tool_call_id: str) -> str:
    digest = hashlib.sha256(f"item:{tool_call_id}".encode()).hexdigest()[:32]
    return f"item_hotline_{digest}"


def _tool_event_id(tool_call_id: str, phase: str) -> str:
    digest = hashlib.sha256(f"{phase}:{tool_call_id}".encode()).hexdigest()[:32]
    return f"evt_hotline_{digest}"


def _batch_event_id(tool_calls: list[tuple[str, str, str]]) -> str:
    call_ids = ":".join(tool_call_id for tool_call_id, _name, _arguments in tool_calls)
    digest = hashlib.sha256(f"batch-response:{call_ids}".encode()).hexdigest()[:32]
    return f"evt_hotline_{digest}"


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _caller_from_sip_headers(sip_headers: Mapping[str, str]) -> str:
    raw_from = sip_headers.get("from")
    if raw_from is None:
        raise IncomingCallError("SIP From header is missing", sip_status=400)
    decoded = unquote(raw_from)
    match = _SIP_PHONE_RE.search(decoded)
    if match is None:
        raise IncomingCallError("SIP caller number is invalid", sip_status=403)
    try:
        return normalize_e164(match.group(1))
    except PhoneNumberError as exc:
        raise IncomingCallError("SIP caller number is invalid", sip_status=403) from exc


def _extract_function_calls(event: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    event_type = event.get("type")
    # Per-item terminal events are also emitted for cancelled and interrupted
    # Responses. Dispatch only from a completed response.done envelope so a
    # partial utterance can never trigger a side effect.
    if event_type != "response.done" or not _response_is_completed(event):
        return []
    response = event.get("response")
    output = response.get("output") if isinstance(response, Mapping) else None
    candidates = (
        [item for item in output if isinstance(item, Mapping)] if isinstance(output, list) else []
    )
    result: list[tuple[str, str, str]] = []
    for item in candidates:
        if item.get("type") not in {None, "function_call"}:
            continue
        call_id = item.get("call_id")
        name = item.get("name")
        arguments = item.get("arguments")
        if (
            isinstance(call_id, str)
            and _CALL_ID_RE.fullmatch(call_id)
            and isinstance(name, str)
            and 1 <= len(name) <= 100
            and isinstance(arguments, str)
        ):
            result.append((call_id, name, arguments))
    return result


def _load_tool_arguments(arguments_json: str) -> dict[str, Any]:
    if len(arguments_json.encode("utf-8")) > _MAX_TOOL_ARGUMENT_BYTES:
        raise ValueError("tool arguments exceed the limit")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("tool arguments contain duplicate object keys")
            result[key] = value
        return result

    payload = json.loads(arguments_json, object_pairs_hook=no_duplicates)
    if not isinstance(payload, dict):
        raise ValueError("tool arguments must be an object")
    return payload


def _header_value(headers: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    for key, value in headers.items():
        if key.casefold() == folded:
            return value
    return None


def _path_call_id(call_id: str) -> str:
    if _CALL_ID_RE.fullmatch(call_id) is None:
        raise ValueError("call_id is invalid")
    return quote(call_id, safe="")


def _required_string(
    arguments: Mapping[str, Any],
    name: str,
    *,
    maximum: int,
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _reject_extra_arguments(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    extra = set(arguments) - allowed
    if extra:
        raise ValueError(f"unexpected tool arguments: {sorted(extra)}")


def _require_no_arguments(arguments: Mapping[str, Any]) -> None:
    if arguments:
        raise ValueError("this tool accepts no arguments")


def _ok(payload: Mapping[str, Any]) -> ToolDispatchResult:
    return ToolDispatchResult(payload={"ok": True, **dict(payload)})


def _normalize_spoken_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _spoken_text_matches(expected: str, actual: str) -> bool:
    expected_normalized = _normalize_spoken_text(expected)
    return bool(expected_normalized) and expected_normalized == _normalize_spoken_text(actual)


def _completed_response_transcript(
    event: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    response = event.get("response")
    if not isinstance(response, Mapping):
        return None, None
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        return None, None
    output = response.get("output")
    if not isinstance(output, list):
        return response_id, None
    transcripts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, Mapping):
                continue
            transcript = part.get("transcript")
            if isinstance(transcript, str) and transcript:
                transcripts.append(transcript)
    return response_id, " ".join(transcripts) or None


def _response_is_completed(event: Mapping[str, Any]) -> bool:
    response = event.get("response")
    return isinstance(response, Mapping) and response.get("status") == "completed"


def _error_payload(message: str, *, retryable: bool) -> dict[str, Any]:
    return {"ok": False, "error": message[:500], "retryable": retryable}


def _safe_tool_error(exc: Exception, *, fallback: str) -> str:
    message = sanitize_untrusted_text(
        str(exc),
        max_chars=400,
        known_secrets=(),
    )
    return message or fallback


__all__ = [
    "IncomingCallResult",
    "OpenAIRealtimeAPIError",
    "OpenAIRealtimeClient",
    "OpenAIRealtimeError",
    "OpenAIRealtimeManager",
    "OpenAIWebhookVerificationError",
    "RealtimeConversation",
    "RealtimeToolDispatcher",
]
