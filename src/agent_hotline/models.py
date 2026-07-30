"""Validated domain models for Agent Hotline.

The models in this module are intentionally provider-neutral.  Provider payloads
are normalized at the HTTP boundary before they reach these types, which keeps
the durable store small, predictable, and free of raw phone numbers or secrets.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Self
from uuid import uuid4

from pydantic import (
    AfterValidator,
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""

    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


AwareDatetime = Annotated[datetime, AfterValidator(_aware_utc)]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]
OpaqueId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]
EventId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
SessionId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^ses_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
DecisionId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^dec_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ActionId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^act_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
GrantId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^grt_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
FallbackId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^fbk_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
TerminationJobId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^trm_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
TimelineId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^tml_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
OwnerRef = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=7,
        max_length=128,
        pattern=r"^(?:owner|usr)_[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]

_RAW_PHONE_RE = re.compile(r"^\+?\d(?:[\s().-]*\d){8,14}$")
_SENSITIVE_KEYS = {
    "agent_phone_number",
    "api_key",
    "authorization",
    "device_code",
    "otp",
    "owner_phone_number",
    "password",
    "phone",
    "phone_number",
    "private_key",
    "refresh_token",
    "secret",
    "token",
    "user_phone_number",
}
_SENSITIVE_SUFFIXES = (
    "_api_key",
    "_authorization",
    "_otp",
    "_password",
    "_phone",
    "_phone_number",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token",
)


def new_id(prefix: str) -> str:
    """Generate a locally unique opaque identifier."""

    return f"{prefix}_{uuid4().hex}"


def _reject_raw_phone(value: str, *, field_name: str) -> str:
    if _RAW_PHONE_RE.fullmatch(value.strip()):
        raise ValueError(f"{field_name} must not contain a raw phone number")
    return value


def validate_owner_ref(value: str) -> str:
    """Validate a redacted owner reference without retaining a phone number."""

    _reject_raw_phone(value, field_name="owner_ref")
    if not re.fullmatch(r"(?:owner|usr)_[A-Za-z0-9][A-Za-z0-9._:-]*", value):
        raise ValueError("owner_ref must be a redacted owner_*/usr_* reference")
    return value


def _validate_safe_json(value: dict[str, JsonValue], *, path: str = "$") -> dict[str, JsonValue]:
    """Reject obvious secrets and raw phone numbers from durable JSON fields."""

    def walk(item: JsonValue, item_path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = key.strip().lower().replace("-", "_")
                if normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES):
                    raise ValueError(f"sensitive field is not allowed at {item_path}.{key}")
                walk(child, f"{item_path}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, f"{item_path}[{index}]")
        elif isinstance(item, str):
            _reject_raw_phone(item, field_name=item_path)

    walk(value, path)
    # JsonValue already guarantees JSON-compatible values.  This also catches
    # accidental custom subclasses if validation is bypassed during construction.
    json.dumps(value, allow_nan=False)
    return value


class StrictModel(BaseModel):
    """Base class that rejects unknown fields and validates assignments."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class EventSource(StrEnum):
    CODEX_APP_SERVER = "codex_app_server"
    CODEX_MCP = "codex_mcp"
    CODEX_HOOK = "codex_hook"
    CLAUDE_MCP = "claude_mcp"
    CLAUDE_HOOK = "claude_hook"
    WATCHDOG = "watchdog"
    MANUAL = "manual"


class AgentType(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    EXTERNAL = "external"


class EscalationKind(StrEnum):
    APPROVAL = "approval"
    INCIDENT = "incident"
    AUTHENTICATION = "authentication"
    AUTH = "auth"
    BLOCKED = "blocked"
    AMBIGUITY = "ambiguity"
    COMPLETION = "completion"
    STATUS = "status"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class EventState(StrEnum):
    DETECTED = "detected"
    QUEUED = "queued"
    DIALING = "dialing"
    CONNECTED = "connected"
    AWAITING_DECISION = "awaiting_decision"
    FALLBACK_PENDING = "fallback_pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    FAILED = "failed"


TERMINAL_EVENT_STATES = frozenset({EventState.RESOLVED, EventState.EXPIRED, EventState.FAILED})
EVENT_STATE_TRANSITIONS: dict[EventState, frozenset[EventState]] = {
    EventState.DETECTED: frozenset({EventState.QUEUED, EventState.EXPIRED, EventState.FAILED}),
    EventState.QUEUED: frozenset({EventState.DIALING, EventState.EXPIRED, EventState.FAILED}),
    EventState.DIALING: frozenset(
        {
            EventState.QUEUED,
            EventState.CONNECTED,
            EventState.FALLBACK_PENDING,
            EventState.RESOLVED,
            EventState.EXPIRED,
            EventState.FAILED,
        }
    ),
    EventState.CONNECTED: frozenset(
        {
            EventState.AWAITING_DECISION,
            EventState.FALLBACK_PENDING,
            EventState.RESOLVED,
            EventState.EXPIRED,
            EventState.FAILED,
        }
    ),
    EventState.AWAITING_DECISION: frozenset(
        {
            EventState.FALLBACK_PENDING,
            EventState.RESOLVED,
            EventState.EXPIRED,
            EventState.FAILED,
        }
    ),
    EventState.FALLBACK_PENDING: frozenset(
        {EventState.RESOLVED, EventState.EXPIRED, EventState.FAILED}
    ),
    EventState.RESOLVED: frozenset(),
    EventState.EXPIRED: frozenset(),
    EventState.FAILED: frozenset(),
}


class ContactDirection(StrEnum):
    OUTBOUND_ESCALATION = "outbound_escalation"
    INBOUND_CONTROL = "inbound_control"


class ContactChannel(StrEnum):
    VOICE = "voice"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    WEB = "web"


class NoAnswerPolicy(StrEnum):
    PAUSE = "pause"
    TEXT_AND_PAUSE = "text_and_pause"
    RETRY_ONCE = "retry_once"
    CONTINUE_SAFELY = "continue_safely"


class SessionState(StrEnum):
    PENDING = "pending"
    DIALING = "dialing"
    RINGING = "ringing"
    CONNECTED = "connected"
    DISCUSSING = "discussing"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_SESSION_STATES = frozenset(
    {
        SessionState.COMPLETED,
        SessionState.NO_ANSWER,
        SessionState.BUSY,
        SessionState.FAILED,
        SessionState.CANCELLED,
    }
)
SESSION_STATE_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.PENDING: frozenset(
        {
            SessionState.DIALING,
            SessionState.RINGING,
            SessionState.CONNECTED,
            SessionState.COMPLETED,
            SessionState.NO_ANSWER,
            SessionState.BUSY,
            SessionState.FAILED,
            SessionState.CANCELLED,
        }
    ),
    SessionState.DIALING: frozenset(
        {
            SessionState.RINGING,
            SessionState.CONNECTED,
            SessionState.COMPLETED,
            SessionState.NO_ANSWER,
            SessionState.BUSY,
            SessionState.FAILED,
            SessionState.CANCELLED,
        }
    ),
    SessionState.RINGING: frozenset(
        {
            SessionState.CONNECTED,
            SessionState.COMPLETED,
            SessionState.NO_ANSWER,
            SessionState.BUSY,
            SessionState.FAILED,
            SessionState.CANCELLED,
        }
    ),
    SessionState.CONNECTED: frozenset(
        {
            SessionState.DISCUSSING,
            SessionState.COMPLETED,
            SessionState.FAILED,
            SessionState.CANCELLED,
        }
    ),
    SessionState.DISCUSSING: frozenset(
        {SessionState.COMPLETED, SessionState.FAILED, SessionState.CANCELLED}
    ),
    SessionState.COMPLETED: frozenset(),
    SessionState.NO_ANSWER: frozenset(),
    SessionState.BUSY: frozenset(),
    SessionState.FAILED: frozenset(),
    SessionState.CANCELLED: frozenset(),
}


class DecisionStatus(StrEnum):
    RESOLVED = "resolved"
    NO_ANSWER = "no_answer"
    EXPIRED = "expired"
    FAILED = "failed"


class DecisionOutcome(StrEnum):
    APPROVE = "approve"
    DENY = "deny"
    INSTRUCT = "instruct"
    DEFER = "defer"
    AUTH_COMPLETED = "auth_completed"
    NONE = "none"


class DecisionSource(StrEnum):
    MID_CALL_TOOL = "mid_call_tool"
    INBOUND_TOOL = "inbound_tool"
    SECURE_FALLBACK = "secure_fallback"
    LOCAL_OPERATOR = "local_operator"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionKind(StrEnum):
    THREAD_INSTRUCTION = "thread_instruction"
    INTERRUPT_THREAD = "interrupt_thread"
    RESUME_THREAD = "resume_thread"
    SPAWN_ROOT_THREAD = "spawn_root_thread"
    REGISTERED_RUNBOOK = "registered_runbook"
    SHELL = "shell"
    DEPLOYMENT = "deployment"
    AUTHENTICATION = "authentication"


class ActionState(StrEnum):
    PREPARED = "prepared"
    CONFIRMED = "confirmed"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class GrantState(StrEnum):
    CONFIRMED = "confirmed"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"


class FallbackState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    VERIFIED = "verified"
    CONSUMED = "consumed"
    EXPIRED = "expired"
    REVOKED = "revoked"
    DELIVERY_FAILED = "delivery_failed"


ACTION_STATE_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PREPARED: frozenset(
        {ActionState.CONFIRMED, ActionState.EXPIRED, ActionState.CANCELLED}
    ),
    ActionState.CONFIRMED: frozenset(
        {ActionState.CONSUMED, ActionState.EXPIRED, ActionState.CANCELLED}
    ),
    ActionState.CONSUMED: frozenset(),
    ActionState.EXPIRED: frozenset(),
    ActionState.CANCELLED: frozenset(),
}


class ConfirmationMethod(StrEnum):
    SPOKEN_PHRASE = "spoken_phrase"
    DTMF_PIN = "dtmf_pin"
    SIGNED_TOKEN = "signed_token"
    TRUSTED_LOCAL = "trusted_local"
    NOT_REQUIRED = "not_required"


class WebhookStatus(StrEnum):
    CONNECTED = "connected"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"


class CallTerminationLeg(StrEnum):
    OPENAI = "openai"
    CARRIER = "carrier"


class CallTerminationState(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"


class TranscriptRole(StrEnum):
    OWNER = "owner"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"


class TimelineKind(StrEnum):
    EVENT_CREATED = "event_created"
    EVENT_STATE_CHANGED = "event_state_changed"
    SNAPSHOT_SAVED = "snapshot_saved"
    SESSION_CREATED = "session_created"
    SESSION_STATE_CHANGED = "session_state_changed"
    ATTEMPT_LINKED = "attempt_linked"
    INTERACTION_LINKED = "interaction_linked"
    CALL_TERMINATION_REQUESTED = "call_termination_requested"
    CALL_TERMINATION_CONFIRMED = "call_termination_confirmed"
    CALL_TERMINATION_UNKNOWN = "call_termination_unknown"
    DECISION_RECORDED = "decision_recorded"
    WEBHOOK_RECEIVED = "webhook_received"
    ACTION_PREPARED = "action_prepared"
    ACTION_CONFIRMED = "action_confirmed"
    ACTION_CONSUMED = "action_consumed"
    ACTION_EXECUTION_STARTED = "action_execution_started"
    ACTION_EXECUTION_SUCCEEDED = "action_execution_succeeded"
    ACTION_EXECUTION_FAILED = "action_execution_failed"
    ACTION_EXECUTION_UNKNOWN = "action_execution_unknown"
    ACTION_EXPIRED = "action_expired"
    ACTION_CANCELLED = "action_cancelled"
    REPOSITORY_CONTEXT_EXPOSED = "repository_context_exposed"
    FALLBACK_CREATED = "fallback_created"
    FALLBACK_DELIVERED = "fallback_delivered"
    FALLBACK_VERIFIED = "fallback_verified"
    FALLBACK_CONSUMED = "fallback_consumed"
    FALLBACK_EXPIRED = "fallback_expired"
    FALLBACK_REVOKED = "fallback_revoked"
    FALLBACK_DELIVERY_FAILED = "fallback_delivery_failed"


class ProposedAction(StrictModel):
    id: OpaqueId
    label: ShortText
    risk: Annotated[str, StringConstraints(strip_whitespace=True, max_length=500)] = ""


class ContextReference(StrictModel):
    type: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=64,
            pattern=r"^[a-z][a-z0-9_]*$",
        ),
    ]
    value: ShortText

    @field_validator("value")
    @classmethod
    def no_phone_in_reference(cls, value: str) -> str:
        return _reject_raw_phone(value, field_name="context reference")


class EscalationEvent(StrictModel):
    event_id: EventId = Field(default_factory=lambda: new_id("evt"))
    source: EventSource = EventSource.MANUAL
    agent_type: AgentType = AgentType.EXTERNAL
    host_id: ShortText = "local"
    thread_id: OpaqueId | None = None
    turn_id: OpaqueId | None = None
    workspace: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None
    kind: EscalationKind
    severity: Severity = Severity.WARNING
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    question: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ] = None
    proposed_actions: Annotated[list[ProposedAction], Field(max_length=20)] = Field(
        default_factory=list
    )
    requested_capabilities: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(
                    strip_whitespace=True,
                    min_length=1,
                    max_length=64,
                    pattern=r"^[a-z][a-z0-9_.:-]*$",
                ),
            ]
        ],
        Field(max_length=30),
    ] = Field(default_factory=list)
    context_refs: Annotated[list[ContextReference], Field(max_length=50)] = Field(
        default_factory=list
    )
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    pending_request: dict[str, JsonValue] = Field(default_factory=dict)
    blocking: bool = True
    preferred_channels: Annotated[list[ContactChannel], Field(min_length=1, max_length=4)] = Field(
        default_factory=lambda: [ContactChannel.VOICE]
    )
    no_answer_policy: NoAnswerPolicy = NoAnswerPolicy.PAUSE
    detected_at: AwareDatetime = Field(default_factory=utc_now)
    deadline_at: AwareDatetime | None = None
    dedupe_key: Annotated[
        str | None,
        StringConstraints(strip_whitespace=True, min_length=3, max_length=300),
    ] = None
    state: EventState = EventState.DETECTED

    @field_validator("summary", "question")
    @classmethod
    def no_phone_in_narrative(cls, value: str | None) -> str | None:
        if value is not None:
            _reject_raw_phone(value, field_name="event narrative")
        return value

    @field_validator("evidence", "pending_request")
    @classmethod
    def safe_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_safe_json(value)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.dedupe_key is None:
            self.dedupe_key = self.event_id
        if self.deadline_at is not None and self.deadline_at <= self.detected_at:
            raise ValueError("deadline_at must be after detected_at")
        if len(set(self.preferred_channels)) != len(self.preferred_channels):
            raise ValueError("preferred_channels must not contain duplicates")
        if (
            self.kind
            in {
                EscalationKind.APPROVAL,
                EscalationKind.AUTH,
                EscalationKind.AUTHENTICATION,
                EscalationKind.BLOCKED,
                EscalationKind.AMBIGUITY,
            }
            and not self.question
        ):
            raise ValueError(f"question is required for {self.kind.value} events")
        return self


class ThreadSnapshot(StrictModel):
    id: OpaqueId
    name: ShortText
    workspace: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ]
    branch: ShortText | None = None
    commit: Annotated[
        str | None,
        StringConstraints(
            strip_whitespace=True,
            min_length=4,
            max_length=128,
            pattern=r"^[A-Za-z0-9._/-]+$",
        ),
    ] = None
    dirty: bool | None = None
    status: ShortText


class TestSnapshot(StrictModel):
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(default=0, ge=0)
    command: ShortText
    finished_at: AwareDatetime | None = None


class IncidentSnapshot(StrictModel):
    service: ShortText
    status: int | ShortText
    attempts: int = Field(default=1, ge=1)
    first_seen_at: AwareDatetime | None = None
    detail: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None


class PendingActionSnapshot(StrictModel):
    action: Annotated[
        str,
        Field(
            validation_alias=AliasChoices("action", "command"),
            serialization_alias="action",
        ),
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]
    next_step: Annotated[
        str | None,
        Field(
            default=None,
            validation_alias=AliasChoices("next_step", "next"),
            serialization_alias="next_step",
        ),
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ]


class ContextSnapshot(StrictModel):
    case_id: EventId = Field(validation_alias=AliasChoices("case_id", "event_id"))
    agent: AgentType
    thread: ThreadSnapshot | None = None
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
    agent_summary: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2500)
    ]
    changed_files: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
            ]
        ],
        Field(max_length=50),
    ] = Field(default_factory=list)
    diff_summary: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
            ]
        ],
        Field(max_length=20),
    ] = Field(default_factory=list)
    tests: TestSnapshot | None = None
    incident: IncidentSnapshot | None = None
    pending_action: PendingActionSnapshot | None = None
    human_constraints: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
            ]
        ],
        Field(max_length=20),
    ] = Field(default_factory=list)
    evidence: dict[str, JsonValue] = Field(default_factory=dict)
    captured_at: AwareDatetime = Field(default_factory=utc_now)

    @property
    def event_id(self) -> str:
        return self.case_id

    @field_validator("evidence")
    @classmethod
    def safe_evidence(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_safe_json(value)


class ContactSession(StrictModel):
    session_id: SessionId = Field(default_factory=lambda: new_id("ses"))
    event_id: EventId | None = None
    owner_ref: OwnerRef = "owner_primary"
    direction: ContactDirection
    channel: ContactChannel = ContactChannel.VOICE
    state: SessionState = SessionState.PENDING
    attempt_id: OpaqueId | None = None
    interaction_id: OpaqueId | None = None
    provider: ShortText = "openai_realtime"
    started_at: AwareDatetime = Field(default_factory=utc_now)
    answered_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    failure_reason: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None

    @field_validator("owner_ref")
    @classmethod
    def redacted_owner_only(cls, value: str) -> str:
        return validate_owner_ref(value)

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> Self:
        if self.answered_at is not None and self.answered_at < self.started_at:
            raise ValueError("answered_at must not precede started_at")
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("ended_at must not precede started_at")
        if (
            self.answered_at is not None
            and self.ended_at is not None
            and self.ended_at < self.answered_at
        ):
            raise ValueError("ended_at must not precede answered_at")
        return self


class CallTerminationJob(StrictModel):
    """Durable, provider-specific work required to end one call leg."""

    job_id: TerminationJobId = Field(default_factory=lambda: new_id("trm"))
    session_id: SessionId | None = None
    event_id: EventId | None = None
    leg: CallTerminationLeg
    target_id: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=3,
            max_length=300,
            pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
        ),
    ]
    state: CallTerminationState = CallTerminationState.PENDING
    attempts: Annotated[int, Field(ge=0)] = 0
    next_attempt_at: AwareDatetime = Field(default_factory=utc_now)
    last_attempt_at: AwareDatetime | None = None
    last_error_type: Annotated[
        str | None,
        StringConstraints(
            strip_whitespace=True,
            min_length=1,
            max_length=160,
            pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$",
        ),
    ] = None
    confirmed_at: AwareDatetime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def valid_attempt_history(self) -> Self:
        if self.attempts == 0 and self.last_attempt_at is not None:
            raise ValueError("an unattempted termination job cannot have last_attempt_at")
        if self.attempts > 0 and self.last_attempt_at is None:
            raise ValueError("an attempted termination job requires last_attempt_at")
        if self.state is CallTerminationState.CONFIRMED and self.confirmed_at is None:
            raise ValueError("a confirmed termination job requires confirmed_at")
        if self.state is CallTerminationState.PENDING and self.confirmed_at is not None:
            raise ValueError("a pending termination job cannot have confirmed_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        return self


class ApprovedAction(StrictModel):
    action_id: ActionId | None = None
    action_type: ActionKind = Field(
        validation_alias=AliasChoices("action_type", "type", "kind"),
        serialization_alias="type",
    )
    exact: ShortText | None = None
    target: ShortText | None = None
    workspace: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None
    thread_id: OpaqueId | None = None
    commit: ShortText | None = None
    environment: ShortText | None = None
    expires_at: AwareDatetime


class Decision(StrictModel):
    decision_id: DecisionId = Field(default_factory=lambda: new_id("dec"))
    event_id: EventId
    session_id: SessionId | None = None
    status: DecisionStatus = DecisionStatus.RESOLVED
    outcome: DecisionOutcome
    instruction: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ] = None
    approved_action_ids: Annotated[list[ActionId], Field(max_length=30)] = Field(
        default_factory=list
    )
    approved_actions: Annotated[list[ApprovedAction], Field(max_length=30)] = Field(
        default_factory=list
    )
    constraints: Annotated[
        list[
            Annotated[
                str,
                StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
            ]
        ],
        Field(max_length=30),
    ] = Field(default_factory=list)
    transcript_summary: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)
    ] = None
    channel: ContactChannel = ContactChannel.VOICE
    identity_verified: bool = False
    source: DecisionSource = DecisionSource.MID_CALL_TOOL
    decided_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is not DecisionStatus.RESOLVED and self.outcome is not DecisionOutcome.NONE:
            raise ValueError("non-resolved decisions cannot grant or imply an outcome")
        if self.outcome is not DecisionOutcome.NONE and not self.identity_verified:
            raise ValueError("every resolved decision outcome requires verified identity")
        if self.outcome is DecisionOutcome.INSTRUCT and not self.instruction:
            raise ValueError("instruction is required for an instruct outcome")
        if (self.approved_action_ids or self.approved_actions) and not self.identity_verified:
            raise ValueError("approved actions require verified identity")
        if len(set(self.approved_action_ids)) != len(self.approved_action_ids):
            raise ValueError("approved_action_ids must not contain duplicates")
        return self


class ActionScope(StrictModel):
    host_id: ShortText
    workspace: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None
    thread_id: OpaqueId | None = None
    commit: ShortText | None = None
    environment: ShortText | None = None


class PreparedAction(StrictModel):
    action_id: ActionId = Field(default_factory=lambda: new_id("act"))
    event_id: EventId
    session_id: SessionId | None = None
    kind: ActionKind
    target: ShortText
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    scope: ActionScope
    risk: RiskLevel
    action_hash: Sha256Hex
    confirmation_phrase_hash: Sha256Hex | None = None
    requires_confirmation: bool = True
    state: ActionState = ActionState.PREPARED
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime

    @field_validator("parameters")
    @classmethod
    def safe_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_safe_json(value)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.risk in {RiskLevel.HIGH, RiskLevel.CRITICAL} and not self.requires_confirmation:
            raise ValueError("high-risk actions require confirmation")
        if self.requires_confirmation and self.confirmation_phrase_hash is None:
            raise ValueError("confirmation_phrase_hash is required when confirmation is required")
        if not self.requires_confirmation and self.confirmation_phrase_hash is not None:
            raise ValueError(
                "confirmation_phrase_hash must be omitted when confirmation is not required"
            )
        return self


class ActionGrant(StrictModel):
    grant_id: GrantId = Field(default_factory=lambda: new_id("grt"))
    action_id: ActionId
    event_id: EventId
    session_id: SessionId | None = None
    action_hash: Sha256Hex
    owner_ref: OwnerRef
    confirmation_method: ConfirmationMethod
    state: GrantState = GrantState.CONFIRMED
    confirmed_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    consumed_at: AwareDatetime | None = None

    @field_validator("owner_ref")
    @classmethod
    def redacted_owner_only(cls, value: str) -> str:
        return validate_owner_ref(value)

    @model_validator(mode="after")
    def validate_grant(self) -> Self:
        if self.expires_at <= self.confirmed_at:
            raise ValueError("expires_at must be after confirmed_at")
        if self.state is GrantState.CONSUMED and self.consumed_at is None:
            raise ValueError("consumed grants require consumed_at")
        if self.state is not GrantState.CONSUMED and self.consumed_at is not None:
            raise ValueError("only consumed grants may have consumed_at")
        if self.consumed_at is not None and self.consumed_at < self.confirmed_at:
            raise ValueError("consumed_at must not precede confirmed_at")
        return self


class FallbackLink(StrictModel):
    fallback_id: FallbackId = Field(default_factory=lambda: new_id("fbk"))
    event_id: EventId
    session_id: SessionId
    state: FallbackState = FallbackState.PENDING
    reason: ShortText
    delivery_channel: ContactChannel = ContactChannel.WEB
    verification_attempts: int = Field(default=0, ge=0, le=20)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    expires_at: AwareDatetime
    delivered_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    consumed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("fallback expiry must be after creation")
        if self.delivered_at is not None and self.delivered_at < self.created_at:
            raise ValueError("fallback delivery cannot precede creation")
        if self.verified_at is not None and self.delivered_at is None:
            raise ValueError("verified fallback requires delivery")
        if self.consumed_at is not None and self.verified_at is None:
            raise ValueError("consumed fallback requires verification")
        if self.state is FallbackState.PENDING and self.delivered_at is not None:
            raise ValueError("pending fallback cannot have a delivery timestamp")
        if (
            self.state
            in {
                FallbackState.DELIVERED,
                FallbackState.VERIFIED,
                FallbackState.CONSUMED,
            }
            and self.delivered_at is None
        ):
            raise ValueError(f"{self.state.value} fallback requires delivery")
        if self.state in {FallbackState.VERIFIED, FallbackState.CONSUMED} and (
            self.verified_at is None
        ):
            raise ValueError(f"{self.state.value} fallback requires verification")
        if self.state is FallbackState.CONSUMED and self.consumed_at is None:
            raise ValueError("consumed fallback requires consumption timestamp")
        if self.state is not FallbackState.CONSUMED and self.consumed_at is not None:
            raise ValueError("only consumed fallback may have a consumption timestamp")
        return self


class TranscriptTurn(StrictModel):
    role: TranscriptRole
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    occurred_at: AwareDatetime | None = None


class ProviderWebhookPayload(StrictModel):
    """Provider-neutral terminal call payload accepted by the durable coordinator."""

    webhook_id: OpaqueId | None = None
    attempt_id: OpaqueId
    interaction_id: OpaqueId | None = None
    status: WebhookStatus
    provider: ShortText = "openai_realtime"
    channel: ContactChannel = ContactChannel.VOICE
    duration_seconds: float | None = Field(default=None, ge=0, le=86_400)
    failure_reason: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = None
    final_agent_variables: dict[str, JsonValue] = Field(default_factory=dict)
    transcript: Annotated[list[TranscriptTurn], Field(max_length=500)] = Field(default_factory=list)
    transcript_summary: Annotated[
        str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)
    ] = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("final_agent_variables", "metadata")
    @classmethod
    def safe_json(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_safe_json(value)


class TimelineEntry(StrictModel):
    timeline_id: TimelineId = Field(default_factory=lambda: new_id("tml"))
    event_id: EventId | None = None
    session_id: SessionId | None = None
    action_id: ActionId | None = None
    kind: TimelineKind
    from_state: ShortText | None = None
    to_state: ShortText | None = None
    details: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("details")
    @classmethod
    def safe_details(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _validate_safe_json(value)


class EventCreateResult(StrictModel):
    event: EscalationEvent
    created: bool
    deduplicated_by: Annotated[
        str | None, StringConstraints(pattern=r"^(?:event_id|dedupe_key)$")
    ] = None


class WebhookReceipt(StrictModel):
    webhook_key: ShortText
    event_id: EventId | None = None
    session_id: SessionId
    created: bool
    status: WebhookStatus


def canonical_model_json(model: BaseModel, *, exclude: set[str] | None = None) -> str:
    """Return stable JSON used for idempotency comparisons and hashing."""

    value: Any = model.model_dump(mode="json", exclude=exclude or set(), by_alias=True)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "ACTION_STATE_TRANSITIONS",
    "EVENT_STATE_TRANSITIONS",
    "SESSION_STATE_TRANSITIONS",
    "TERMINAL_EVENT_STATES",
    "TERMINAL_SESSION_STATES",
    "ActionGrant",
    "ActionId",
    "ActionKind",
    "ActionScope",
    "ActionState",
    "AgentType",
    "ApprovedAction",
    "AwareDatetime",
    "CallTerminationJob",
    "CallTerminationLeg",
    "CallTerminationState",
    "ConfirmationMethod",
    "ContactChannel",
    "ContactDirection",
    "ContactSession",
    "ContextReference",
    "ContextSnapshot",
    "Decision",
    "DecisionOutcome",
    "DecisionSource",
    "DecisionStatus",
    "EscalationEvent",
    "EscalationKind",
    "EventCreateResult",
    "EventId",
    "EventSource",
    "EventState",
    "FallbackId",
    "FallbackLink",
    "FallbackState",
    "GrantState",
    "IncidentSnapshot",
    "NoAnswerPolicy",
    "PendingActionSnapshot",
    "PreparedAction",
    "ProposedAction",
    "ProviderWebhookPayload",
    "RiskLevel",
    "SessionState",
    "Severity",
    "TerminationJobId",
    "TestSnapshot",
    "ThreadSnapshot",
    "TimelineEntry",
    "TimelineKind",
    "TranscriptRole",
    "TranscriptTurn",
    "WebhookReceipt",
    "WebhookStatus",
    "canonical_model_json",
    "new_id",
    "utc_now",
    "validate_owner_ref",
]
