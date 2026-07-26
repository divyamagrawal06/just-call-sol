from __future__ import annotations

from dataclasses import dataclass

import pytest

from agent_hotline.codex_bridge import (
    COMMAND_APPROVAL,
    FILE_APPROVAL,
    PERMISSIONS_APPROVAL,
    VoiceApprovalHandler,
)
from agent_hotline.codex_protocol import CodexServerRequest, ServerRequestFailure
from agent_hotline.contracts import ContactHumanRequest, ContactHumanResult


@dataclass
class StubCoordinator:
    result: ContactHumanResult | None = None
    error: Exception | None = None
    request: ContactHumanRequest | None = None

    async def contact_human(self, request: ContactHumanRequest) -> ContactHumanResult:
        self.request = request
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def _result(
    *,
    status: str,
    outcome: str,
    identity_verified: bool,
) -> ContactHumanResult:
    return ContactHumanResult(
        event_id="evt_voice-approval-test",
        status=status,
        outcome=outcome,
        identity_verified=identity_verified,
    )


def _request(method: str, params: object | None = None) -> CodexServerRequest:
    return CodexServerRequest(
        request_id=1,
        method=method,
        params=(
            params
            if params is not None
            else {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "reason": "The test needs one bounded operation.",
                "command": ["uv", "run", "pytest"],
                "cwd": "C:/workspace/project",
            }
        ),
    )


@pytest.mark.parametrize(
    ("status", "outcome", "identity_verified", "expected"),
    [
        ("resolved", "approve", True, "accept"),
        ("resolved", "approve", False, "decline"),
        ("resolved", "auth_completed", True, "decline"),
        ("resolved", "deny", True, "decline"),
        ("resolved", "instruct", True, "decline"),
        ("timed_out", "approve", True, "decline"),
        ("no_answer", "none", False, "decline"),
        ("busy", "none", False, "decline"),
        ("failed", "none", False, "decline"),
    ],
)
async def test_command_approval_accepts_only_verified_explicit_approval(
    status: str,
    outcome: str,
    identity_verified: bool,
    expected: str,
) -> None:
    coordinator = StubCoordinator(
        result=_result(
            status=status,
            outcome=outcome,
            identity_verified=identity_verified,
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(COMMAND_APPROVAL))

    assert response == {"decision": expected}
    assert coordinator.request is not None
    assert coordinator.request.kind == "approval"
    assert coordinator.request.wait_for_decision is True
    assert coordinator.request.context.pending_action_summary is not None
    assert "session" not in coordinator.request.question.casefold()


async def test_file_approval_uses_the_same_verified_one_shot_policy() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    accepted = await handler.handle(
        _request(
            FILE_APPROVAL,
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "reason": "Update one generated fixture.",
                "grantRoot": "C:/workspace/project",
            },
        )
    )
    coordinator.result = _result(
        status="resolved",
        outcome="approve",
        identity_verified=False,
    )
    declined = await handler.handle(_request(FILE_APPROVAL))

    assert accepted == {"decision": "accept"}
    assert declined == {"decision": "decline"}


async def test_dedupe_key_binds_the_exact_approval_request() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="deny", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    await handler.handle(
        _request(COMMAND_APPROVAL, {"command": ["tool", "first"], "reason": "first"})
    )
    assert coordinator.request is not None
    first_key = coordinator.request.dedupe_key
    await handler.handle(
        _request(COMMAND_APPROVAL, {"command": ["tool", "second"], "reason": "second"})
    )
    assert coordinator.request is not None
    second_key = coordinator.request.dedupe_key

    assert first_key != second_key


async def test_permission_approval_returns_at_most_the_requested_subset() -> None:
    permissions = {
        "network": {"enabled": True},
        "fileSystem": {"read": ["C:/workspace/project"]},
    }
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]
    request = _request(
        PERMISSIONS_APPROVAL,
        {
            "threadId": "thread-1",
            "reason": "Read a fixture and reach the test server.",
            "permissions": permissions,
        },
    )

    accepted = await handler.handle(request)
    coordinator.result = _result(
        status="resolved",
        outcome="approve",
        identity_verified=False,
    )
    declined = await handler.handle(request)

    assert accepted == {"scope": "turn", "permissions": permissions}
    assert declined == {"scope": "turn", "permissions": {}}


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        (COMMAND_APPROVAL, {"decision": "decline"}),
        (FILE_APPROVAL, {"decision": "decline"}),
        (PERMISSIONS_APPROVAL, {"scope": "turn", "permissions": {}}),
    ],
)
async def test_adapter_failures_return_protocol_valid_denials(
    method: str,
    expected: dict[str, object],
) -> None:
    handler = VoiceApprovalHandler(  # type: ignore[arg-type]
        StubCoordinator(error=RuntimeError("provider failed"))
    )

    assert await handler.handle(_request(method)) == expected


async def test_unknown_or_malformed_callbacks_are_never_treated_as_approval() -> None:
    handler = VoiceApprovalHandler(  # type: ignore[arg-type]
        StubCoordinator(
            result=_result(status="resolved", outcome="approve", identity_verified=True)
        )
    )

    with pytest.raises(ServerRequestFailure) as unsupported:
        await handler.handle(_request("item/unknown/requestApproval"))
    with pytest.raises(ServerRequestFailure) as malformed:
        await handler.handle(_request(COMMAND_APPROVAL, ["not", "an", "object"]))

    assert unsupported.value.code == -32601
    assert malformed.value.code == -32602
