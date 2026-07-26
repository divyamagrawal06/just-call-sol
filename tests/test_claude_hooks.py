from __future__ import annotations

import io
import json
import sys
from typing import Any

from agent_hotline import claude_hooks
from agent_hotline.claude_hooks import (
    MAX_HOOK_INPUT_BYTES,
    build_hook_dispatch,
    deliver_hook,
    parse_hook_input,
)
from agent_hotline.contracts import ContactHumanRequest, NotifyHumanRequest


def _event(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "session_id": "session-secret-identifier",
        "cwd": "C:/work/project",
        "hook_event_name": name,
        **extra,
    }


def test_permission_request_alert_cannot_grant_permission() -> None:
    dispatch = build_hook_dispatch(
        _event(
            "PermissionRequest",
            tool_name="Bash",
            tool_input={
                "command": "deploy --token should-never-appear",
                "description": "Deploy the staging service",
            },
            permission_suggestions=[
                {
                    "type": "addRules",
                    "behavior": "allow",
                    "rules": [{"toolName": "Bash"}],
                }
            ],
        )
    )

    assert dispatch is not None
    assert dispatch.route == "contact"
    request = dispatch.request
    assert isinstance(request, ContactHumanRequest)
    assert request.source == "claude_hook"
    assert request.kind == "approval"
    assert request.wait_for_decision is False
    assert request.no_answer_policy == "notify_only"
    assert request.proposed_actions == []
    serialized = request.model_dump_json()
    assert "should-never-appear" not in serialized
    assert "permission_suggestions" not in serialized
    assert "does not grant" in request.question


def test_permission_denied_only_notifies_and_never_retries() -> None:
    dispatch = build_hook_dispatch(
        _event(
            "PermissionDenied",
            permission_mode="auto",
            tool_name="Write",
            reason="Blocked by classifier",
        )
    )

    assert dispatch is not None
    assert dispatch.route == "notify"
    assert isinstance(dispatch.request, NotifyHumanRequest)
    assert "retry" not in dispatch.request.model_dump()
    assert "does not retry or approve" in dispatch.request.question


def test_tool_failure_redacts_credentials_and_phone_numbers(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "known-anthropic-secret")
    dispatch = build_hook_dispatch(
        _event(
            "PostToolUseFailure",
            tool_name="Bash",
            tool_input={
                "description": "Call +1 415 555 1212 with token=known-anthropic-secret",
                "command": "curl -H 'Authorization: Bearer known-anthropic-secret'",
            },
            error=("authentication failed for +1 415 555 1212; api_key=known-anthropic-secret"),
            is_interrupt=False,
        )
    )

    assert dispatch is not None
    assert dispatch.route == "contact"
    request = dispatch.request
    assert request.kind == "authentication"
    serialized = request.model_dump_json()
    assert "known-anthropic-secret" not in serialized
    assert "+1 415 555 1212" not in serialized
    assert "password" in request.question
    assert request.context.thread_id != "session-secret-identifier"


def test_general_tool_failure_is_nonblocking_notification() -> None:
    dispatch = build_hook_dispatch(
        _event(
            "PostToolUseFailure",
            tool_name="Read",
            tool_input={"file_path": "src/missing.py"},
            error="File does not exist",
        )
    )

    assert dispatch is not None
    assert dispatch.route == "notify"
    assert isinstance(dispatch.request, NotifyHumanRequest)
    assert dispatch.request.kind == "incident"


def test_stop_failure_maps_auth_and_provider_events() -> None:
    auth = build_hook_dispatch(
        _event(
            "StopFailure",
            error="authentication_failed",
            error_details="OAuth session expired",
        )
    )
    rate_limit = build_hook_dispatch(
        _event(
            "StopFailure",
            error="rate_limit",
            error_details="429 Too Many Requests",
        )
    )

    assert auth is not None
    assert auth.request.kind == "authentication"
    assert rate_limit is not None
    assert rate_limit.request.kind == "provider_failure"
    assert auth.request.wait_for_decision is False
    assert rate_limit.request.wait_for_decision is False


def test_stop_only_escalates_when_human_input_is_explicit() -> None:
    completed = build_hook_dispatch(
        _event(
            "Stop",
            stop_hook_active=False,
            last_assistant_message="Implementation complete and tests pass.",
            background_tasks=[],
            session_crons=[],
        )
    )
    marked = build_hook_dispatch(
        _event(
            "Stop",
            stop_hook_active=False,
            last_assistant_message="[HOTLINE] I need your approval before continuing.",
            background_tasks=[],
            session_crons=[],
        )
    )
    recursive = build_hook_dispatch(
        _event(
            "Stop",
            stop_hook_active=True,
            last_assistant_message="[HOTLINE] Call again.",
        )
    )
    background = build_hook_dispatch(
        _event(
            "Stop",
            stop_hook_active=False,
            last_assistant_message="[HOTLINE] Waiting for your input.",
            background_tasks=[{"id": "task-1", "status": "running"}],
        )
    )

    assert completed is None
    assert marked is not None
    assert marked.route == "contact"
    assert recursive is None
    assert background is None


def test_unknown_and_malformed_input_are_ignored() -> None:
    assert build_hook_dispatch(_event("SessionStart")) is None
    assert build_hook_dispatch({}) is None
    assert parse_hook_input(io.BytesIO(b"not-json")) is None
    assert parse_hook_input(io.BytesIO(b"[]")) is None
    assert parse_hook_input(io.BytesIO(b"x" * (MAX_HOOK_INPUT_BYTES + 1))) is None


class _RecordingSink:
    def __init__(self) -> None:
        self.contacts: list[ContactHumanRequest] = []
        self.notifications: list[NotifyHumanRequest] = []

    async def contact_human(self, request: ContactHumanRequest) -> object:
        self.contacts.append(request)
        return object()

    async def notify_human(self, request: NotifyHumanRequest) -> object:
        self.notifications.append(request)
        return object()


async def test_deliver_hook_uses_only_selected_non_authoritative_route() -> None:
    contact_dispatch = build_hook_dispatch(
        _event("PermissionRequest", tool_name="Bash", tool_input={})
    )
    notify_dispatch = build_hook_dispatch(
        _event(
            "PostToolUseFailure",
            tool_name="Read",
            tool_input={},
            error="missing file",
        )
    )
    assert contact_dispatch is not None
    assert notify_dispatch is not None

    sink = _RecordingSink()
    await deliver_hook(contact_dispatch, sink=sink)
    await deliver_hook(notify_dispatch, sink=sink)

    assert len(sink.contacts) == 1
    assert len(sink.notifications) == 1


def test_main_fails_open_without_hook_control_output(monkeypatch, capsys) -> None:
    payload = _event("StopFailure", error="rate_limit")

    class _Stdin:
        buffer = io.BytesIO(json.dumps(payload).encode())

    async def _fail_delivery(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("daemon unavailable")

    monkeypatch.setattr(sys, "stdin", _Stdin())
    monkeypatch.setattr(claude_hooks, "deliver_hook", _fail_delivery)

    assert claude_hooks.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
