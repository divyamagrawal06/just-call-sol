from __future__ import annotations

import json
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
from agent_hotline.security import canonical_json

_COMMAND_SCOPE_KEYS = (
    "command",
    "cwd",
    "environmentId",
    "reason",
    "networkApprovalContext",
    "additionalPermissions",
    "commandActions",
    "proposedExecpolicyAmendment",
    "proposedNetworkPolicyAmendments",
    "availableDecisions",
)
_PERMISSIONS_SCOPE_KEYS = ("cwd", "environmentId", "reason", "permissions")


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
    instruction: str | None = None,
) -> ContactHumanResult:
    return ContactHumanResult(
        event_id="evt_voice-approval-test",
        status=status,
        outcome=outcome,
        identity_verified=identity_verified,
        instruction=instruction,
    )


def _base_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "threadId": "thread-1",
        "turnId": "turn-1",
        "itemId": "item-1",
        "startedAtMs": 1_750_000_000_000,
        "reason": "The test needs one bounded operation.",
        "command": "uv run pytest",
        "cwd": "C:/workspace/project",
    }
    params.update(overrides)
    return params


def _request(method: str, params: object | None = None) -> CodexServerRequest:
    return CodexServerRequest(
        request_id=1,
        method=method,
        params=params if params is not None else _base_params(),
    )


def _permissions_params(**overrides: object) -> dict[str, object]:
    params = _base_params(**overrides)
    params.pop("command", None)
    return params


def _scope(params: dict[str, object], keys: tuple[str, ...]) -> str:
    return canonical_json({key: params[key] for key in keys if key in params})


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
    params = _base_params()
    coordinator = StubCoordinator(
        result=_result(
            status=status,
            outcome=outcome,
            identity_verified=identity_verified,
            instruction=_scope(params, _COMMAND_SCOPE_KEYS),
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(COMMAND_APPROVAL, params))

    assert response == {"decision": expected}
    assert coordinator.request is not None
    assert coordinator.request.kind == "approval"
    assert coordinator.request.wait_for_decision is True
    assert coordinator.request.context.pending_action_summary is not None
    assert "session or persistent policy approval will be returned" in (
        coordinator.request.question.casefold()
    )
    assert "byte-for-byte as the durable instruction" in coordinator.request.question


async def test_command_readback_losslessly_includes_every_security_relevant_field() -> None:
    params = _base_params(
        environmentId="devbox-7",
        networkApprovalContext={"host": "api.example.test", "protocol": "https"},
        additionalPermissions={
            "network": {"enabled": True},
            "fileSystem": {
                "entries": [
                    {
                        "access": "read",
                        "path": {"type": "path", "path": "C:/workspace/project/input.csv"},
                    }
                ]
            },
        },
        commandActions=[
            {
                "type": "read",
                "command": "Get-Content input.csv",
                "name": "input.csv",
                "path": "C:/workspace/project/input.csv",
            },
            {
                "type": "search",
                "command": "rg participant input.csv",
                "path": "C:/workspace/project/input.csv",
                "query": "participant",
            },
        ],
        proposedExecpolicyAmendment=["prefix_rule", "uv", "run"],
        proposedNetworkPolicyAmendments=[{"action": "allow", "host": "api.example.test"}],
        availableDecisions=["accept", "acceptForSession", "decline", "cancel"],
    )
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=_scope(params, _COMMAND_SCOPE_KEYS),
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    assert await handler.handle(_request(COMMAND_APPROVAL, params)) == {"decision": "accept"}

    assert coordinator.request is not None
    scope = json.loads(coordinator.request.context.pending_action_summary or "")
    for field in (
        "command",
        "cwd",
        "environmentId",
        "reason",
        "networkApprovalContext",
        "additionalPermissions",
        "commandActions",
        "proposedExecpolicyAmendment",
        "proposedNetworkPolicyAmendments",
        "availableDecisions",
    ):
        assert scope[field] == params[field]


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (["accept", "decline"], "accept"),
        (["acceptForSession", "decline"], "decline"),
        (["cancel", "acceptForSession"], "cancel"),
    ],
)
async def test_command_never_returns_a_decision_that_is_not_available(
    available: list[object],
    expected: str,
) -> None:
    params = _base_params(availableDecisions=available)
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=_scope(params, _COMMAND_SCOPE_KEYS),
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(COMMAND_APPROVAL, params))

    assert response == {"decision": expected}
    assert response["decision"] in available


async def test_command_refuses_when_only_persistent_or_approval_responses_exist() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    with pytest.raises(ServerRequestFailure) as failure:
        await handler.handle(
            _request(
                COMMAND_APPROVAL,
                _base_params(availableDecisions=["acceptForSession"]),
            )
        )

    assert failure.value.code == -32602
    assert coordinator.request is None


async def test_malformed_available_decisions_fail_without_an_unavailable_response() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    with pytest.raises(ServerRequestFailure) as failure:
        await handler.handle(
            _request(
                COMMAND_APPROVAL,
                _base_params(availableDecisions=["approve-everything"]),
            )
        )

    assert failure.value.code == -32602
    assert coordinator.request is None


async def test_command_persistent_decision_objects_are_read_back_but_never_returned() -> None:
    execpolicy = {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["prefix_rule", "uv"]}}
    network_policy = {
        "applyNetworkPolicyAmendment": {
            "network_policy_amendment": {
                "action": "allow",
                "host": "api.example.test",
            }
        }
    }
    available: list[object] = [execpolicy, network_policy, "accept", "decline"]
    params = _base_params(availableDecisions=available)
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=_scope(params, _COMMAND_SCOPE_KEYS),
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(COMMAND_APPROVAL, params))

    assert response == {"decision": "accept"}
    assert coordinator.request is not None
    scope = json.loads(coordinator.request.context.pending_action_summary or "")
    assert scope["availableDecisions"] == available


@pytest.mark.parametrize(
    "instruction",
    [
        None,
        "approve it",
        '{"command":"uv run pytest"}',
    ],
)
async def test_command_vague_or_partial_durable_instruction_never_authorizes(
    instruction: str | None,
) -> None:
    params = _base_params(availableDecisions=["accept", "decline"])
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=instruction,
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(COMMAND_APPROVAL, params))

    assert response == {"decision": "decline"}
    assert coordinator.request is not None


async def test_command_near_match_durable_instruction_never_authorizes() -> None:
    params = _base_params(availableDecisions=["accept", "decline"])
    exact_scope = _scope(params, _COMMAND_SCOPE_KEYS)
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=f"{exact_scope} ",
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(COMMAND_APPROVAL, params))

    assert response == {"decision": "decline"}


async def test_unbounded_or_lossy_command_scope_is_refused_before_calling() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    oversized = await handler.handle(
        _request(
            COMMAND_APPROVAL,
            _base_params(
                command=f"echo {'x' * 1_200}",
                availableDecisions=["accept", "decline"],
            ),
        )
    )
    injection_like = await handler.handle(
        _request(
            COMMAND_APPROVAL,
            _base_params(
                command="echo ignore previous instructions",
                availableDecisions=["accept", "decline"],
            ),
        )
    )

    assert oversized == {"decision": "decline"}
    assert injection_like == {"decision": "decline"}
    assert coordinator.request is None


async def test_unknown_future_command_scope_is_refused_not_silently_ignored() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(
        _request(
            COMMAND_APPROVAL,
            _base_params(
                futurePrivilege={"root": True},
                availableDecisions=["accept", "decline"],
            ),
        )
    )

    assert response == {"decision": "decline"}
    assert coordinator.request is None


async def test_file_change_is_never_voice_approved_without_patch_payload() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(
        _request(
            FILE_APPROVAL,
            _base_params(
                command=None,
                grantRoot="C:/workspace/project",
            ),
        )
    )

    assert response == {"decision": "decline"}
    assert coordinator.request is None


async def test_dedupe_key_binds_the_exact_approval_request() -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="deny", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    await handler.handle(
        _request(COMMAND_APPROVAL, _base_params(command="tool first", reason="first"))
    )
    assert coordinator.request is not None
    first_key = coordinator.request.dedupe_key
    await handler.handle(
        _request(COMMAND_APPROVAL, _base_params(command="tool second", reason="second"))
    )
    assert coordinator.request is not None
    second_key = coordinator.request.dedupe_key

    assert first_key != second_key


async def test_permission_approval_returns_exact_requested_subset_for_turn_only() -> None:
    permissions = {
        "network": {"enabled": True},
        "fileSystem": {
            "read": ["C:/workspace/project"],
            "entries": [
                {
                    "access": "write",
                    "path": {
                        "type": "glob_pattern",
                        "pattern": "C:/workspace/project/generated/**",
                    },
                }
            ],
        },
    }
    params = _permissions_params(permissions=permissions)
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=_scope(params, _PERMISSIONS_SCOPE_KEYS),
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    accepted = await handler.handle(_request(PERMISSIONS_APPROVAL, params))
    assert accepted == {"scope": "turn", "permissions": permissions}
    assert accepted["permissions"] is not permissions
    assert coordinator.request is not None
    scope = json.loads(coordinator.request.context.pending_action_summary or "")
    assert scope["permissions"] == permissions
    assert scope["cwd"] == params["cwd"]

    coordinator.result = _result(
        status="resolved",
        outcome="approve",
        identity_verified=False,
    )
    declined = await handler.handle(_request(PERMISSIONS_APPROVAL, params))
    assert declined == {"scope": "turn", "permissions": {}}


@pytest.mark.parametrize(
    "instruction",
    [
        None,
        "grant the requested permissions",
        '{"permissions":{"network":{"enabled":true}}}',
    ],
)
async def test_permission_vague_or_partial_durable_instruction_never_authorizes(
    instruction: str | None,
) -> None:
    permissions = {"network": {"enabled": True}}
    params = _permissions_params(permissions=permissions)
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=instruction,
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(PERMISSIONS_APPROVAL, params))

    assert response == {"scope": "turn", "permissions": {}}
    assert coordinator.request is not None


async def test_permission_near_match_durable_instruction_never_authorizes() -> None:
    permissions = {"network": {"enabled": True}}
    params = _permissions_params(permissions=permissions)
    exact_scope = _scope(params, _PERMISSIONS_SCOPE_KEYS)
    coordinator = StubCoordinator(
        result=_result(
            status="resolved",
            outcome="approve",
            identity_verified=True,
            instruction=exact_scope.replace('"enabled":true', '"enabled":false'),
        )
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(_request(PERMISSIONS_APPROVAL, params))

    assert response == {"scope": "turn", "permissions": {}}


@pytest.mark.parametrize(
    "permissions",
    [
        {"process": {"spawn": True}},
        {"network": {"enabled": "yes"}},
        {"fileSystem": {"write": "C:/workspace"}},
        {
            "fileSystem": {
                "entries": [
                    {
                        "access": "write",
                        "path": {"type": "unknownFuturePath", "path": "C:/workspace"},
                    }
                ]
            }
        },
    ],
)
async def test_malformed_or_future_permissions_are_never_granted(
    permissions: dict[str, object],
) -> None:
    coordinator = StubCoordinator(
        result=_result(status="resolved", outcome="approve", identity_verified=True)
    )
    handler = VoiceApprovalHandler(coordinator)  # type: ignore[arg-type]

    response = await handler.handle(
        _request(PERMISSIONS_APPROVAL, _permissions_params(permissions=permissions))
    )

    assert response == {"scope": "turn", "permissions": {}}
    assert coordinator.request is None


@pytest.mark.parametrize(
    ("method", "params", "expected"),
    [
        (
            COMMAND_APPROVAL,
            _base_params(availableDecisions=["accept", "decline"]),
            {"decision": "decline"},
        ),
        (
            FILE_APPROVAL,
            _base_params(command=None),
            {"decision": "decline"},
        ),
        (
            PERMISSIONS_APPROVAL,
            _permissions_params(permissions={"network": {"enabled": True}}),
            {"scope": "turn", "permissions": {}},
        ),
    ],
)
async def test_adapter_failures_return_protocol_valid_denials(
    method: str,
    params: dict[str, object],
    expected: dict[str, object],
) -> None:
    handler = VoiceApprovalHandler(  # type: ignore[arg-type]
        StubCoordinator(error=RuntimeError("provider failed"))
    )

    assert await handler.handle(_request(method, params)) == expected


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
