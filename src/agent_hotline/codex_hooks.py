"""Phone-backed decisions for Codex ``PermissionRequest`` hooks.

Codex writes one hook event as JSON on stdin.  This adapter calls the local
Hotline daemon synchronously and emits a Codex decision only for a verified,
exact, one-shot approval or an explicit denial.  Every malformed, unsupported,
ambiguous, or unavailable path emits nothing so Codex keeps its normal local
approval prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, BinaryIO, Protocol

from .client import HotlineClient
from .contracts import ContactHumanRequest, ContactHumanResult, ContextPacket, EvidenceReference
from .security import canonical_json, sanitize_untrusted_text

MAX_HOOK_INPUT_BYTES = 65_536
MAX_SCOPE_JSON_CHARS = 1_150
DECISION_TIMEOUT_SECONDS = 600

_ALLOWED_INPUT_KEYS = {
    "cwd",
    "hook_event_name",
    "model",
    "permission_mode",
    "session_id",
    "tool_input",
    "tool_name",
    "transcript_path",
    "turn_id",
}
_PERMISSION_MODES = {
    "default",
    "acceptEdits",
    "plan",
    "dontAsk",
    "bypassPermissions",
}
_SAFE_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_KNOWN_SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "HOTLINE_ACTION_SIGNING_SECRET",
    "HOTLINE_FALLBACK_SIGNING_SECRET",
    "HOTLINE_FALLBACK_WEBHOOK_TOKEN",
    "HOTLINE_LOCAL_TOKEN",
    "HOTLINE_SIP_CORRELATION_SECRET",
    "OPENAI_API_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "OWNER_CONFIRMATION_PIN",
    "TWILIO_AUTH_TOKEN",
)


class HookSink(Protocol):
    async def contact_human(self, request: ContactHumanRequest) -> ContactHumanResult: ...


@dataclass(frozen=True, slots=True)
class PermissionDispatch:
    """A validated Hotline request bound to one exact Codex permission scope."""

    scope: str
    request: ContactHumanRequest


def parse_hook_input(stream: BinaryIO) -> Mapping[str, Any] | None:
    """Read one bounded JSON object without following the transcript path."""

    raw = stream.read(MAX_HOOK_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_HOOK_INPUT_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return payload if isinstance(payload, Mapping) else None


def build_permission_dispatch(payload: Mapping[str, Any]) -> PermissionDispatch | None:
    """Build a lossless, bounded request for supported Codex approval events."""

    try:
        if set(payload) - _ALLOWED_INPUT_KEYS:
            return None
        if payload.get("hook_event_name") != "PermissionRequest":
            return None

        session_id = _required_string(payload, "session_id", 200)
        turn_id = _required_string(payload, "turn_id", 200)
        cwd = _required_string(payload, "cwd", 2_000)
        model = _required_string(payload, "model", 200)
        permission_mode = _required_string(payload, "permission_mode", 40)
        tool_name = _required_string(payload, "tool_name", 160)
        if permission_mode not in _PERMISSION_MODES or not _SAFE_TOOL_NAME.fullmatch(tool_name):
            return None

        transcript_path = payload.get("transcript_path")
        if transcript_path is not None and (
            not isinstance(transcript_path, str) or len(transcript_path) > 4_000
        ):
            return None

        # File edits remain on Codex's normal local approval path.  Although the
        # hook exposes a patch command, reading a patch back naturally and
        # losslessly over PSTN is not a dependable authorization experience.
        if tool_name == "apply_patch":
            return None
        if tool_name != "Bash" and not tool_name.startswith("mcp__"):
            return None

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            return None
        if tool_name == "Bash":
            command = tool_input.get("command")
            if not isinstance(command, str) or not command.strip():
                return None

        scope_document = {
            "cwd": cwd,
            "model": model,
            "permission_mode": permission_mode,
            "session_id": session_id,
            "tool_input": dict(tool_input),
            "tool_name": tool_name,
            "turn_id": turn_id,
        }
        scope = canonical_json(scope_document)
        if len(scope) > MAX_SCOPE_JSON_CHARS:
            return None
        safe_scope = sanitize_untrusted_text(
            scope,
            max_chars=MAX_SCOPE_JSON_CHARS + 64,
            known_secrets=_known_secret_values(),
        )
        if safe_scope != scope:
            return None

        question = (
            "Approve or deny this exact one-time Codex permission. You can answer naturally. "
            "If you approve, the voice agent must record this exact JSON byte-for-byte as the "
            "durable instruction. No session or persistent approval will be granted. "
            f"Required instruction: {scope}"
        )
        request = ContactHumanRequest(
            source="codex_hook",
            kind="approval",
            severity="high",
            summary=f"Codex is waiting for one {tool_name} approval.",
            question=question,
            context=ContextPacket(
                thread_id=session_id,
                workspace_ref=_workspace_ref(cwd),
                task_summary=f"Codex requested one bounded permission for {tool_name}.",
                pending_action_summary=scope,
                owner_constraints=[
                    "One tool invocation only; never grant session or persistent access.",
                    "Any change to the tool, arguments, workspace, task, or turn needs a new call.",
                ],
                evidence=[
                    EvidenceReference(
                        kind="thread",
                        ref=turn_id,
                        summary="The exact permission scope is in pending_action_summary.",
                    )
                ],
            ),
            no_answer_policy="pause",
            wait_for_decision=True,
            timeout_seconds=DECISION_TIMEOUT_SECONDS,
        )
        return PermissionDispatch(scope=scope, request=request)
    except (TypeError, ValueError, RecursionError):
        return None


def decision_output(result: ContactHumanResult, *, expected_scope: str) -> dict[str, Any] | None:
    """Convert only a verified exact terminal result into Codex hook control JSON."""

    if result.status != "resolved" or not result.identity_verified:
        return None
    if result.outcome == "approve" and result.instruction == expected_scope:
        return _hook_decision("allow")
    if result.outcome == "deny":
        return _hook_decision("deny", message="Denied by the verified owner via Better Call Sol.")
    return None


async def resolve_permission_dispatch(
    dispatch: PermissionDispatch,
    *,
    sink: HookSink | None = None,
) -> dict[str, Any] | None:
    """Wait for the owner and return a bounded Codex decision, if one exists."""

    if sink is not None:
        result = await sink.contact_human(dispatch.request)
    else:
        async with HotlineClient() as client:
            result = await client.contact_human(dispatch.request)
    return decision_output(result, expected_scope=dispatch.scope)


def main() -> int:
    """Console entry point. Fail open to Codex's ordinary approval UI."""

    try:
        payload = parse_hook_input(sys.stdin.buffer)
        if payload is None:
            return 0
        dispatch = build_permission_dispatch(payload)
        if dispatch is None:
            return 0
        output = asyncio.run(resolve_permission_dispatch(dispatch))
        if output is not None:
            sys.stdout.write(json.dumps(output, separators=(",", ":")) + "\n")
    except (Exception, KeyboardInterrupt):
        # The phone path must never suppress Codex's normal local approval flow.
        return 0
    return 0


def _required_string(payload: Mapping[str, Any], field: str, max_chars: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise ValueError(f"{field} must be a bounded non-empty string")
    return value


def _workspace_ref(cwd: str) -> str:
    normalized = cwd.replace("\\", "/").rstrip("/")
    return (PurePath(normalized).name or "workspace")[:200]


def _known_secret_values() -> tuple[str, ...]:
    return tuple(
        value
        for name in _KNOWN_SECRET_ENV_NAMES
        if (value := os.environ.get(name)) and len(value) >= 4
    )


def _hook_decision(behavior: str, *, message: str | None = None) -> dict[str, Any]:
    decision: dict[str, str] = {"behavior": behavior}
    if message is not None:
        decision["message"] = message
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


if __name__ == "__main__":
    raise SystemExit(main())
