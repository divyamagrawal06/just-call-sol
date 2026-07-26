"""Fail-open Claude Code hook adapter for Agent Hotline.

Claude Code writes one hook event as JSON on stdin. This adapter deliberately emits
nothing on stdout and always exits successfully: it can alert the owner, but it cannot
allow, deny, retry, block, or continue a Claude turn.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO, Literal, Protocol

from .client import HotlineClient
from .contracts import (
    ContactHumanRequest,
    ContextPacket,
    EvidenceReference,
    NotifyHumanRequest,
)
from .security import sanitize_untrusted_text

MAX_HOOK_INPUT_BYTES = 1_048_576
DEFAULT_DELIVERY_TIMEOUT_SECONDS = 8.0
_STOP_MARKER = re.compile(r"\[\s*hotline\s*]", re.IGNORECASE)
_STOP_NEEDS_HUMAN = re.compile(
    r"\b(?:blocked|waiting\s+for\s+(?:your|human)|"
    r"need(?:s|ed)?\s+(?:your|human)\s+(?:approval|input|decision)|"
    r"please\s+(?:approve|sign\s*in|authenticate))\b",
    re.IGNORECASE,
)
_AUTH_FAILURE = re.compile(
    r"\b(?:auth(?:entication|orization)?|oauth|sign[ -]?in|log[ -]?in|"
    r"credential|unauthorized|forbidden|401|403|mfa)\b",
    re.IGNORECASE,
)
_PROVIDER_FAILURE = re.compile(
    r"\b(?:rate[ -]?limit|quota|capacity|overload|billing|credit|"
    r"too many requests|429|provider|model not found)\b",
    re.IGNORECASE,
)
_KNOWN_SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "HOTLINE_CALLBACK_TOKEN",
    "HOTLINE_LOCAL_TOKEN",
    "HOTLINE_TOOL_TOKEN",
    "SARVAM_API_KEY",
)


class HookSink(Protocol):
    async def contact_human(self, request: ContactHumanRequest) -> object: ...

    async def notify_human(self, request: NotifyHumanRequest) -> object: ...


@dataclass(frozen=True, slots=True)
class HookDispatch:
    """One sanitized, non-authoritative daemon submission."""

    route: Literal["contact", "notify"]
    request: ContactHumanRequest | NotifyHumanRequest


def parse_hook_input(stream: BinaryIO) -> Mapping[str, Any] | None:
    """Read one bounded JSON object without following transcript paths."""

    raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_HOOK_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def build_hook_dispatch(payload: Mapping[str, Any]) -> HookDispatch | None:
    """Translate an allowlisted Claude hook event into a sanitized Hotline request."""

    event_name = payload.get("hook_event_name")
    if not isinstance(event_name, str):
        return None

    if event_name == "PermissionRequest":
        return _permission_request(payload)
    if event_name == "PermissionDenied":
        return _permission_denied(payload)
    if event_name == "PostToolUseFailure":
        return _tool_failure(payload)
    if event_name == "StopFailure":
        return _stop_failure(payload)
    if event_name == "Stop":
        return _stop(payload)
    return None


async def deliver_hook(
    dispatch: HookDispatch,
    *,
    sink: HookSink | None = None,
    timeout_seconds: float = DEFAULT_DELIVERY_TIMEOUT_SECONDS,
) -> None:
    """Submit without surfacing a decision back into Claude's hook protocol."""

    if timeout_seconds <= 0:
        return
    if sink is not None:
        async with asyncio.timeout(timeout_seconds):
            await _send(dispatch, sink)
        return

    async with HotlineClient() as client:
        async with asyncio.timeout(timeout_seconds):
            await _send(dispatch, client)


async def _send(dispatch: HookDispatch, sink: HookSink) -> None:
    if dispatch.route == "contact":
        await sink.contact_human(ContactHumanRequest.model_validate(dispatch.request))
    else:
        await sink.notify_human(NotifyHumanRequest.model_validate(dispatch.request))


def main() -> int:
    """Console entry point. Fail open and produce no hook-control output."""

    try:
        payload = parse_hook_input(sys.stdin.buffer)
        if payload is None:
            return 0
        dispatch = build_hook_dispatch(payload)
        if dispatch is None:
            return 0
        asyncio.run(
            deliver_hook(
                dispatch,
                timeout_seconds=_delivery_timeout_from_environment(),
            )
        )
    except (Exception, KeyboardInterrupt):
        # A telemetry/alert path must never break or decide a Claude action.
        return 0
    return 0


def _permission_request(payload: Mapping[str, Any]) -> HookDispatch:
    tool = _safe_tool_name(payload.get("tool_name"))
    description = _tool_description(payload)
    context = _base_context(
        payload,
        task_summary=f"Claude requested local permission for {tool}.",
        agent_summary=description,
    )
    return HookDispatch(
        route="contact",
        request=ContactHumanRequest(
            source="claude_hook",
            kind="approval",
            severity="medium",
            summary=f"Claude is waiting at a local permission dialog for {tool}.",
            question=(
                "Review the exact request in Claude Code and approve or deny it there. "
                "This phone alert does not grant the permission."
            ),
            context=context,
            dedupe_key=_dedupe_key(payload, "permission-request", tool),
            no_answer_policy="notify_only",
            wait_for_decision=False,
            timeout_seconds=1,
        ),
    )


def _permission_denied(payload: Mapping[str, Any]) -> HookDispatch:
    tool = _safe_tool_name(payload.get("tool_name"))
    reason = _safe_text(payload.get("reason"), 1000) or "The permission policy denied it."
    return HookDispatch(
        route="notify",
        request=NotifyHumanRequest(
            source="claude_hook",
            kind="approval",
            severity="low",
            summary=f"Claude's permission policy denied a request for {tool}.",
            question=(
                "Inspect the Claude session if the task should continue. "
                "This alert does not retry or approve the denied request."
            ),
            context=_base_context(
                payload,
                task_summary=f"Permission denied for {tool}.",
                last_error=reason,
            ),
            dedupe_key=_dedupe_key(payload, "permission-denied", tool),
            no_answer_policy="notify_only",
        ),
    )


def _tool_failure(payload: Mapping[str, Any]) -> HookDispatch:
    tool = _safe_tool_name(payload.get("tool_name"))
    error = _safe_text(payload.get("error"), 2500) or "The tool failed without details."
    is_interrupt = payload.get("is_interrupt") is True

    if _AUTH_FAILURE.search(error):
        kind = "authentication"
        severity = "high"
        route: Literal["contact", "notify"] = "contact"
        question = (
            "Complete any sign-in only in the legitimate browser or device flow, "
            "then resume Claude. Never dictate a password, OTP, or recovery code."
        )
    elif _PROVIDER_FAILURE.search(error):
        kind = "provider_failure"
        severity = "high"
        route = "contact"
        question = "Review the provider failure and decide whether to retry, defer, or stop."
    elif is_interrupt:
        kind = "other"
        severity = "low"
        route = "notify"
        question = "Inspect the Claude session if the interrupted operation should be retried."
    else:
        kind = "incident"
        severity = "medium"
        route = "notify"
        question = (
            "Inspect the failed tool in Claude and decide whether the task needs intervention."
        )

    request_fields: dict[str, Any] = {
        "source": "claude_hook",
        "kind": kind,
        "severity": severity,
        "summary": f"Claude's {tool} tool failed.",
        "question": question,
        "context": _base_context(
            payload,
            task_summary=f"Failed Claude tool: {tool}.",
            agent_summary=_tool_description(payload),
            last_error=error,
        ),
        "dedupe_key": _dedupe_key(payload, "tool-failure", f"{tool}:{error[:120]}"),
        "no_answer_policy": "notify_only",
    }
    if route == "contact":
        request_fields.update(wait_for_decision=False, timeout_seconds=1)
        request: ContactHumanRequest | NotifyHumanRequest = ContactHumanRequest(**request_fields)
    else:
        request = NotifyHumanRequest(**request_fields)
    return HookDispatch(route=route, request=request)


def _stop_failure(payload: Mapping[str, Any]) -> HookDispatch:
    error_type = _safe_identifier(payload.get("error"), "unknown")
    details = (
        " ".join(
            part
            for part in (
                _safe_text(payload.get("error_details"), 1500),
                _safe_text(payload.get("last_assistant_message"), 1000),
            )
            if part
        )
        or f"Claude stopped because of {error_type}."
    )

    if error_type in {"authentication_failed", "oauth_org_not_allowed"}:
        kind = "authentication"
        question = (
            "Restore access through the legitimate provider sign-in flow, then resume "
            "Claude. Do not share credentials or codes by voice."
        )
    else:
        kind = "provider_failure"
        question = "Decide whether to retry Claude later, defer the task, or investigate now."

    return HookDispatch(
        route="contact",
        request=ContactHumanRequest(
            source="claude_hook",
            kind=kind,
            severity="high",
            summary=f"Claude stopped after an API failure ({error_type}).",
            question=question,
            context=_base_context(
                payload,
                task_summary="Claude could not complete its current response.",
                last_error=details,
            ),
            dedupe_key=_dedupe_key(payload, "stop-failure", error_type),
            no_answer_policy="notify_only",
            wait_for_decision=False,
            timeout_seconds=1,
        ),
    )


def _stop(payload: Mapping[str, Any]) -> HookDispatch | None:
    if payload.get("stop_hook_active") is True:
        return None
    if _has_active_background_work(payload):
        return None

    message = _safe_text(payload.get("last_assistant_message"), 2500)
    if not message or not (_STOP_MARKER.search(message) or _STOP_NEEDS_HUMAN.search(message)):
        return None
    message = _STOP_MARKER.sub("", message).strip()
    if not message:
        message = "Claude requested owner input before continuing."

    kind = "authentication" if _AUTH_FAILURE.search(message) else "clarification"
    question = (
        "Complete sign-in only through the legitimate browser or device flow, then "
        "resume Claude. Do not share credentials or codes by voice."
        if kind == "authentication"
        else "Review Claude's final message and continue the session with your instruction."
    )
    return HookDispatch(
        route="contact",
        request=ContactHumanRequest(
            source="claude_hook",
            kind=kind,
            severity="medium",
            summary="Claude stopped because it needs owner input.",
            question=question,
            context=_base_context(
                payload,
                task_summary="Claude paused at the end of a response.",
                agent_summary=message,
            ),
            dedupe_key=_dedupe_key(payload, "stop", message[:120]),
            no_answer_policy="notify_only",
            wait_for_decision=False,
            timeout_seconds=1,
        ),
    )


def _base_context(
    payload: Mapping[str, Any],
    *,
    task_summary: str,
    agent_summary: str | None = None,
    last_error: str | None = None,
) -> ContextPacket:
    session_alias = _session_alias(payload.get("session_id"))
    workspace = _safe_text(payload.get("cwd"), 500)
    evidence_summary = last_error or agent_summary or task_summary
    return ContextPacket(
        thread_id=f"claude:{session_alias}",
        thread_alias=f"Claude session {session_alias}",
        workspace_ref=workspace,
        task_summary=_safe_text(task_summary, 2000),
        agent_summary=_safe_text(agent_summary, 4000),
        last_error=_safe_text(last_error, 3000),
        owner_constraints=[
            "Hook alerts never approve, deny, retry, or continue a Claude action.",
            "Review permission details in Claude Code before acting.",
        ],
        evidence=[
            EvidenceReference(
                kind="error" if last_error else "thread",
                ref=f"claude:{session_alias}",
                summary=_safe_text(evidence_summary, 1000) or "Claude hook event received.",
            )
        ],
    )


def _tool_description(payload: Mapping[str, Any]) -> str | None:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return None
    for field in ("description", "task", "purpose"):
        description = _safe_text(tool_input.get(field), 600)
        if description:
            return description
    file_path = _safe_text(tool_input.get("file_path"), 500)
    return f"Target file: {file_path}" if file_path else None


def _safe_tool_name(value: object) -> str:
    if not isinstance(value, str):
        return "unknown tool"
    value = re.sub(r"[^A-Za-z0-9_.:-]", "", value)[:100]
    return value or "unknown tool"


def _safe_identifier(value: object, default: str) -> str:
    if not isinstance(value, str):
        return default
    value = re.sub(r"[^a-z0-9_-]", "-", value.lower())[:80].strip("-")
    return value or default


def _safe_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    safe = sanitize_untrusted_text(
        value,
        max_chars=max(64, limit),
        known_secrets=_known_secret_values(),
    )
    safe = " ".join(safe.split())
    return safe[:limit] or None


def _known_secret_values() -> tuple[str, ...]:
    return tuple(
        value
        for name in _KNOWN_SECRET_ENV_NAMES
        if (value := os.environ.get(name)) and len(value) >= 4
    )


def _session_alias(value: object) -> str:
    raw = value if isinstance(value, str) else "unknown"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _dedupe_key(payload: Mapping[str, Any], category: str, discriminator: str) -> str:
    basis = json.dumps(
        {
            "session": _session_alias(payload.get("session_id")),
            "prompt": _safe_identifier(payload.get("prompt_id"), ""),
            "category": category,
            "discriminator": discriminator,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"claude-hook-{hashlib.sha256(basis.encode()).hexdigest()[:32]}"


def _has_active_background_work(payload: Mapping[str, Any]) -> bool:
    for field in ("background_tasks", "session_crons"):
        value = payload.get(field)
        if isinstance(value, list) and value:
            return True
    return False


def _delivery_timeout_from_environment() -> float:
    raw = os.environ.get("HOTLINE_CLAUDE_HOOK_TIMEOUT_SECONDS", "")
    try:
        value = float(raw) if raw else DEFAULT_DELIVERY_TIMEOUT_SECONDS
    except ValueError:
        return DEFAULT_DELIVERY_TIMEOUT_SECONDS
    return min(max(value, 1.0), 20.0)


if __name__ == "__main__":
    raise SystemExit(main())
