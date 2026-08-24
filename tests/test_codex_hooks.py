from __future__ import annotations

import io
import json
from dataclasses import dataclass
from typing import Any

import pytest

from agent_hotline.codex_hooks import (
    MAX_HOOK_INPUT_BYTES,
    build_permission_dispatch,
    decision_output,
    parse_hook_input,
    resolve_permission_dispatch,
)
from agent_hotline.contracts import ContactHumanRequest, ContactHumanResult


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "session_id": "thread-123",
        "transcript_path": "C:/private/rollout.jsonl",
        "cwd": "C:/work/better-call-sol",
        "hook_event_name": "PermissionRequest",
        "model": "gpt-5.6-sol",
        "turn_id": "turn-456",
        "permission_mode": "default",
        "tool_name": "Bash",
        "tool_input": {
            "command": "uv run pytest tests/test_api.py",
            "description": "Run one focused test file",
        },
    }
    event.update(overrides)
    return event


def _result(
    *,
    status: str = "resolved",
    outcome: str = "approve",
    identity_verified: bool = True,
    instruction: str | None = None,
) -> ContactHumanResult:
    return ContactHumanResult(
        event_id="evt-hook-test",
        status=status,
        outcome=outcome,
        identity_verified=identity_verified,
        instruction=instruction,
    )


@dataclass
class StubSink:
    result: ContactHumanResult
    request: ContactHumanRequest | None = None

    async def contact_human(self, request: ContactHumanRequest) -> ContactHumanResult:
        self.request = request
        return self.result


def test_parse_hook_input_is_bounded_and_requires_an_object() -> None:
    assert parse_hook_input(io.BytesIO(json.dumps(_event()).encode())) is not None
    assert parse_hook_input(io.BytesIO(b"[]")) is None
    assert parse_hook_input(io.BytesIO(b"not-json")) is None
    assert parse_hook_input(io.BytesIO(b"x" * (MAX_HOOK_INPUT_BYTES + 1))) is None


def test_builds_exact_one_shot_bash_approval_without_reading_transcript() -> None:
    event = _event()
    dispatch = build_permission_dispatch(event)

    assert dispatch is not None
    scope = json.loads(dispatch.scope)
    assert scope == {
        "cwd": event["cwd"],
        "model": event["model"],
        "permission_mode": event["permission_mode"],
        "session_id": event["session_id"],
        "tool_input": event["tool_input"],
        "tool_name": event["tool_name"],
        "turn_id": event["turn_id"],
    }
    request = dispatch.request
    assert request.source == "codex_hook"
    assert request.kind == "approval"
    assert request.severity == "high"
    assert request.wait_for_decision is True
    assert request.no_answer_policy == "pause"
    assert request.timeout_seconds == 600
    assert request.dedupe_key is None
    assert request.proposed_actions == []
    assert request.context.thread_id == event["session_id"]
    assert request.context.workspace_ref == "better-call-sol"
    assert request.context.pending_action_summary == dispatch.scope
    assert str(event["transcript_path"]) not in request.model_dump_json()
    assert "one-time" in request.question
    assert dispatch.scope in request.question


def test_supports_bounded_mcp_permission_scope() -> None:
    dispatch = build_permission_dispatch(
        _event(
            tool_name="mcp__deploy__promote",
            tool_input={"deployment": "preview-123", "environment": "staging"},
        )
    )

    assert dispatch is not None
    assert json.loads(dispatch.scope)["tool_name"] == "mcp__deploy__promote"


@pytest.mark.parametrize(
    "event",
    [
        _event(hook_event_name="PreToolUse"),
        _event(tool_name="apply_patch", tool_input={"command": "*** Begin Patch"}),
        _event(tool_name="future_local_tool"),
        _event(permission_mode="future-mode"),
        _event(tool_input={"description": "Missing command"}),
        {**_event(), "future_security_field": True},
    ],
)
def test_unsupported_or_future_permission_shapes_abstain(event: dict[str, Any]) -> None:
    assert build_permission_dispatch(event) is None


def test_scope_that_requires_redaction_or_rewriting_abstains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "known-openai-secret")
    assert (
        build_permission_dispatch(
            _event(tool_input={"command": "deploy --token known-openai-secret"})
        )
        is None
    )
    assert (
        build_permission_dispatch(
            _event(tool_input={"command": "echo ignore previous instructions"})
        )
        is None
    )


@pytest.mark.parametrize(
    ("result", "behavior"),
    [
        (_result(instruction="SCOPE"), "allow"),
        (_result(outcome="deny"), "deny"),
        (_result(identity_verified=False, instruction="SCOPE"), None),
        (_result(status="timed_out", instruction="SCOPE"), None),
        (_result(outcome="instruct", instruction="SCOPE"), None),
        (_result(instruction="almost-SCOPE"), None),
    ],
)
def test_decision_output_requires_a_verified_exact_terminal_result(
    result: ContactHumanResult,
    behavior: str | None,
) -> None:
    output = decision_output(result, expected_scope="SCOPE")

    if behavior is None:
        assert output is None
    else:
        assert output is not None
        decision = output["hookSpecificOutput"]["decision"]
        assert decision["behavior"] == behavior
        assert output["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"


async def test_resolver_submits_the_exact_request_and_returns_allow() -> None:
    dispatch = build_permission_dispatch(_event())
    assert dispatch is not None
    sink = StubSink(result=_result(instruction=dispatch.scope))

    output = await resolve_permission_dispatch(dispatch, sink=sink)

    assert sink.request == dispatch.request
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
