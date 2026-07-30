"""Codex App Server callbacks routed through the durable voice coordinator.

The callback protocol can ask for approvals broader than a single operation.
This bridge deliberately supports only exact, one-turn grants.  If every
security-relevant field cannot be represented losslessly in the bounded voice
prompt, the request is refused without calling the owner.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from .codex_protocol import CodexServerRequest, JSONValue, ServerRequestFailure
from .contracts import ContactHumanRequest, ContextPacket, EvidenceReference
from .coordinator import HotlineCoordinator
from .security import canonical_json, sanitize_untrusted_text

COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"

_COMMAND_PARAM_KEYS = {
    "additionalPermissions",
    "approvalId",
    "availableDecisions",
    "command",
    "commandActions",
    "cwd",
    "environmentId",
    "itemId",
    "networkApprovalContext",
    "proposedExecpolicyAmendment",
    "proposedNetworkPolicyAmendments",
    "reason",
    "startedAtMs",
    "threadId",
    "turnId",
}
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
_FILE_PARAM_KEYS = {
    "grantRoot",
    "itemId",
    "reason",
    "startedAtMs",
    "threadId",
    "turnId",
}
_PERMISSIONS_PARAM_KEYS = {
    "cwd",
    "environmentId",
    "itemId",
    "permissions",
    "reason",
    "startedAtMs",
    "threadId",
    "turnId",
}
_PERMISSIONS_SCOPE_KEYS = ("cwd", "environmentId", "reason", "permissions")
_ONE_SHOT_COMMAND_DECISIONS = {"accept", "decline", "cancel"}
_ALL_STRING_COMMAND_DECISIONS = _ONE_SHOT_COMMAND_DECISIONS | {"acceptForSession"}
_MAX_SCOPE_JSON_CHARS = 1_150

logger = logging.getLogger(__name__)


class _ScopeNotConveyable(ValueError):
    """The callback cannot be represented exactly and safely over voice."""


class VoiceApprovalHandler:
    """Fail-closed handler for the bounded App Server approval request set."""

    def __init__(self, coordinator: HotlineCoordinator) -> None:
        self.coordinator = coordinator

    async def handle(self, request: CodexServerRequest) -> JSONValue:
        if not isinstance(request.params, Mapping):
            raise ServerRequestFailure(-32602, "Approval request params must be an object")
        params = dict(request.params)
        try:
            if request.method == COMMAND_APPROVAL:
                return await self._command_approval(params)
            if request.method == FILE_APPROVAL:
                return await self._file_approval(params)
            if request.method == PERMISSIONS_APPROVAL:
                return await self._permissions_approval(params)
            raise ServerRequestFailure(-32601, f"Unsupported voice callback {request.method!r}")
        except ServerRequestFailure:
            raise
        except Exception as exc:
            logger.warning(
                "Voice approval failed closed for %s (%s)",
                request.method,
                type(exc).__name__,
            )
            if request.method == COMMAND_APPROVAL:
                return {"decision": _safe_command_denial(params)}
            if request.method == PERMISSIONS_APPROVAL:
                return {"scope": "turn", "permissions": {}}
            return {"decision": "decline"}

    async def _command_approval(self, params: dict[str, Any]) -> JSONValue:
        try:
            _validate_envelope(params, _COMMAND_PARAM_KEYS)
            _validate_command_scope(params)
            available = _available_command_decisions(params.get("availableDecisions"))
            # A provider/backend failure must still have a protocol-valid,
            # non-persistent fail-closed response.
            _safe_command_denial(params)
            scope = _exact_scope_json(params, _COMMAND_SCOPE_KEYS)
        except _ScopeNotConveyable:
            return {"decision": _safe_command_denial(params)}
        if available is not None and "accept" not in available:
            # Voice intentionally cannot choose a persistent approval variant,
            # so there is nothing useful to ask when one-shot accept is absent.
            return {"decision": _safe_command_denial(params)}

        thread_id = _bounded_identifier(params.get("threadId"))
        turn_id = _bounded_identifier(params.get("turnId"))
        result = await self.coordinator.contact_human(
            ContactHumanRequest(
                source="codex_app_server",
                kind="approval",
                severity="high",
                summary="Codex is paused on an exact one-command approval.",
                question=(
                    "To approve this one execution, record this exact JSON "
                    "byte-for-byte as the durable instruction; otherwise decline. "
                    "No session or persistent policy approval will be returned. "
                    f"Required instruction: {scope}"
                ),
                context=ContextPacket(
                    thread_id=thread_id,
                    workspace_ref=_workspace_ref(params.get("cwd")),
                    task_summary="Codex supplied the exact bounded request scope below.",
                    pending_action_summary=scope,
                    evidence=[
                        EvidenceReference(
                            kind="other",
                            ref=turn_id or thread_id or "codex-command-approval",
                            summary="Exact command approval scope is in pending_action_summary.",
                        )
                    ],
                ),
                dedupe_key=_dedupe_key("command", params),
                timeout_seconds=600,
            )
        )
        approved = (
            result.status == "resolved"
            and result.outcome == "approve"
            and result.identity_verified
            and result.instruction == scope
        )
        if approved and _decision_is_available("accept", available):
            return {"decision": "accept"}
        return {"decision": _safe_command_denial(params)}

    async def _file_approval(self, params: dict[str, Any]) -> JSONValue:
        # Codex 0.144.6 supplies only reason/grantRoot metadata here, not the
        # pending patch or an exact file list.  `grantRoot` may additionally
        # represent a session-scoped write grant.  Voice cannot faithfully
        # read back the actual change, so this callback is intentionally not
        # approvable until the protocol includes the change payload.
        with suppress(_ScopeNotConveyable):
            _validate_envelope(params, _FILE_PARAM_KEYS)
        return {"decision": "decline"}

    async def _permissions_approval(self, params: dict[str, Any]) -> JSONValue:
        try:
            _validate_envelope(params, _PERMISSIONS_PARAM_KEYS)
            permissions = params.get("permissions")
            _validate_permission_profile(permissions)
            scope = _exact_scope_json(params, _PERMISSIONS_SCOPE_KEYS)
        except _ScopeNotConveyable:
            return {"scope": "turn", "permissions": {}}

        thread_id = _bounded_identifier(params.get("threadId"))
        result = await self.coordinator.contact_human(
            ContactHumanRequest(
                source="codex_app_server",
                kind="approval",
                severity="high",
                summary="Codex is paused on an exact turn-scoped permission request.",
                question=(
                    "To grant exactly these permissions for this turn only, record "
                    "this exact JSON byte-for-byte as the durable instruction; "
                    f"otherwise decline. Required instruction: {scope}"
                ),
                context=ContextPacket(
                    thread_id=thread_id,
                    workspace_ref=_workspace_ref(params.get("cwd")),
                    task_summary="Codex supplied the exact bounded permission scope below.",
                    pending_action_summary=scope,
                ),
                dedupe_key=_dedupe_key("permissions", params),
                timeout_seconds=600,
            )
        )
        if (
            result.status == "resolved"
            and result.outcome == "approve"
            and result.identity_verified
            and result.instruction == scope
        ):
            # Return a defensive copy of the exact requested profile.  Never
            # synthesize broader entries and never grant session scope.
            assert isinstance(permissions, dict)
            return {"scope": "turn", "permissions": copy.deepcopy(permissions)}
        return {"scope": "turn", "permissions": {}}


def register_voice_callbacks(client: Any, handler: VoiceApprovalHandler) -> None:
    """Register only protocol methods with deterministic response schemas."""

    for method in (COMMAND_APPROVAL, FILE_APPROVAL, PERMISSIONS_APPROVAL):
        client.register_server_request_handler(method, handler)


def _validate_envelope(params: Mapping[str, Any], allowed_keys: set[str]) -> None:
    unknown = set(params) - allowed_keys
    if unknown:
        raise _ScopeNotConveyable(f"unknown callback fields: {sorted(unknown)!r}")
    for name in ("threadId", "turnId", "itemId"):
        value = params.get(name)
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise _ScopeNotConveyable(f"{name} must be a bounded identifier")
    started_at = params.get("startedAtMs")
    if isinstance(started_at, bool) or not isinstance(started_at, int) or started_at < 0:
        raise _ScopeNotConveyable("startedAtMs must be a non-negative integer")


def _validate_command_scope(params: Mapping[str, Any]) -> None:
    _validate_optional_string(params, "approvalId", max_chars=160)
    _validate_optional_string(params, "command", max_chars=_MAX_SCOPE_JSON_CHARS)
    _validate_optional_string(params, "cwd", max_chars=1_000)
    _validate_optional_string(params, "environmentId", max_chars=160)
    _validate_optional_string(params, "reason", max_chars=1_000)

    command = params.get("command")
    network = params.get("networkApprovalContext")
    if not isinstance(command, str) and network is None:
        raise _ScopeNotConveyable("neither command nor network scope was supplied")
    if network is not None:
        _validate_network_context(network)
    additional = params.get("additionalPermissions")
    if additional is not None:
        _validate_permission_profile(additional)
    actions = params.get("commandActions")
    if actions is not None:
        _validate_command_actions(actions)
    amendment = params.get("proposedExecpolicyAmendment")
    if amendment is not None:
        _validate_string_list(amendment, "proposedExecpolicyAmendment")
    network_amendments = params.get("proposedNetworkPolicyAmendments")
    if network_amendments is not None:
        _validate_network_amendments(network_amendments)
    _available_command_decisions(params.get("availableDecisions"))


def _validate_network_context(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {"host", "protocol"}:
        raise _ScopeNotConveyable("networkApprovalContext has an unsupported shape")
    if not isinstance(value.get("host"), str) or not value["host"]:
        raise _ScopeNotConveyable("network host is missing")
    if value.get("protocol") not in {"http", "https", "socks5Tcp", "socks5Udp"}:
        raise _ScopeNotConveyable("network protocol is unsupported")


def _validate_command_actions(value: object) -> None:
    if not isinstance(value, list):
        raise _ScopeNotConveyable("commandActions must be an array")
    shapes = {
        "read": ({"type", "command", "name", "path"}, {"type", "command", "name", "path"}),
        "listFiles": ({"type", "command"}, {"type", "command", "path"}),
        "search": ({"type", "command"}, {"type", "command", "path", "query"}),
        "unknown": ({"type", "command"}, {"type", "command"}),
    }
    for action in value:
        if not isinstance(action, Mapping) or action.get("type") not in shapes:
            raise _ScopeNotConveyable("commandActions contains an unsupported action")
        required, allowed = shapes[str(action["type"])]
        if not required <= set(action) or not set(action) <= allowed:
            raise _ScopeNotConveyable("commandActions contains an unsupported shape")
        if not isinstance(action.get("command"), str):
            raise _ScopeNotConveyable("command action command must be a string")
        for key, item in action.items():
            if key in {"path", "query"} and item is not None and not isinstance(item, str):
                raise _ScopeNotConveyable(f"command action {key} must be a string or null")
            if key == "name" and not isinstance(item, str):
                raise _ScopeNotConveyable("command action name must be a string")


def _validate_permission_profile(value: object) -> None:
    if not isinstance(value, dict) or not set(value) <= {"fileSystem", "network"}:
        raise _ScopeNotConveyable("permission profile has an unsupported shape")
    file_system = value.get("fileSystem")
    if file_system is not None:
        if not isinstance(file_system, dict) or not set(file_system) <= {
            "entries",
            "globScanMaxDepth",
            "read",
            "write",
        }:
            raise _ScopeNotConveyable("file-system permission profile is unsupported")
        if "globScanMaxDepth" in file_system and file_system["globScanMaxDepth"] is not None:
            depth = file_system["globScanMaxDepth"]
            if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
                raise _ScopeNotConveyable("globScanMaxDepth is invalid")
        for field in ("read", "write"):
            if field in file_system and file_system[field] is not None:
                _validate_string_list(file_system[field], field)
        if "entries" in file_system and file_system["entries"] is not None:
            _validate_file_system_entries(file_system["entries"])
    network = value.get("network")
    if network is not None:
        if not isinstance(network, dict) or not set(network) <= {"enabled"}:
            raise _ScopeNotConveyable("network permission profile is unsupported")
        if (
            "enabled" in network
            and network["enabled"] is not None
            and not isinstance(network["enabled"], bool)
        ):
            raise _ScopeNotConveyable("network.enabled must be boolean or null")


def _validate_file_system_entries(value: object) -> None:
    if not isinstance(value, list):
        raise _ScopeNotConveyable("file-system entries must be an array")
    for entry in value:
        if not isinstance(entry, Mapping) or set(entry) != {"access", "path"}:
            raise _ScopeNotConveyable("file-system entry has an unsupported shape")
        if entry.get("access") not in {"read", "write", "deny"}:
            raise _ScopeNotConveyable("file-system access is unsupported")
        path = entry.get("path")
        if not isinstance(path, Mapping):
            raise _ScopeNotConveyable("file-system path is unsupported")
        path_type = path.get("type")
        if path_type == "path":
            valid = set(path) == {"type", "path"} and isinstance(path.get("path"), str)
        elif path_type == "glob_pattern":
            valid = set(path) == {"type", "pattern"} and isinstance(path.get("pattern"), str)
        elif path_type == "special":
            valid = set(path) == {"type", "value"} and _valid_special_path(path.get("value"))
        else:
            valid = False
        if not valid:
            raise _ScopeNotConveyable("file-system path has an unsupported shape")


def _valid_special_path(value: object) -> bool:
    if not isinstance(value, Mapping) or not isinstance(value.get("kind"), str):
        return False
    kind = value["kind"]
    if kind in {"root", "minimal", "tmpdir", "slash_tmp"}:
        return set(value) == {"kind"}
    if kind == "project_roots":
        return set(value) <= {"kind", "subpath"} and (
            value.get("subpath") is None or isinstance(value.get("subpath"), str)
        )
    if kind == "unknown":
        return (
            {"kind", "path"} <= set(value) <= {"kind", "path", "subpath"}
            and isinstance(value.get("path"), str)
            and (value.get("subpath") is None or isinstance(value.get("subpath"), str))
        )
    return False


def _validate_network_amendments(value: object) -> None:
    if not isinstance(value, list):
        raise _ScopeNotConveyable("network policy amendments must be an array")
    for amendment in value:
        if (
            not isinstance(amendment, Mapping)
            or set(amendment) != {"action", "host"}
            or amendment.get("action") not in {"allow", "deny"}
            or not isinstance(amendment.get("host"), str)
            or not amendment.get("host")
        ):
            raise _ScopeNotConveyable("network policy amendment is unsupported")


def _available_command_decisions(value: object) -> set[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise _ScopeNotConveyable("availableDecisions must be an array or null")
    available: set[str] = set()
    for decision in value:
        if isinstance(decision, str) and decision in _ALL_STRING_COMMAND_DECISIONS:
            available.add(decision)
            continue
        if _valid_execpolicy_decision(decision):
            available.add("acceptWithExecpolicyAmendment")
            continue
        if _valid_network_policy_decision(decision):
            available.add("applyNetworkPolicyAmendment")
            continue
        raise _ScopeNotConveyable("availableDecisions contains an unsupported decision")
    return available


def _valid_execpolicy_decision(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"acceptWithExecpolicyAmendment"}:
        return False
    wrapper = value.get("acceptWithExecpolicyAmendment")
    return (
        isinstance(wrapper, Mapping)
        and set(wrapper) == {"execpolicy_amendment"}
        and _is_string_list(wrapper.get("execpolicy_amendment"))
    )


def _valid_network_policy_decision(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"applyNetworkPolicyAmendment"}:
        return False
    wrapper = value.get("applyNetworkPolicyAmendment")
    if not isinstance(wrapper, Mapping) or set(wrapper) != {"network_policy_amendment"}:
        return False
    amendment = wrapper.get("network_policy_amendment")
    return (
        isinstance(amendment, Mapping)
        and set(amendment) == {"action", "host"}
        and amendment.get("action") in {"allow", "deny"}
        and isinstance(amendment.get("host"), str)
        and bool(amendment.get("host"))
    )


def _safe_command_denial(params: Mapping[str, Any]) -> str:
    try:
        available = _available_command_decisions(params.get("availableDecisions"))
    except _ScopeNotConveyable as exc:
        raise ServerRequestFailure(
            -32602,
            "Cannot safely respond to malformed availableDecisions",
        ) from exc
    if available is None or "decline" in available:
        return "decline"
    if "cancel" in available:
        return "cancel"
    raise ServerRequestFailure(
        -32602,
        "No non-persistent denial is present in availableDecisions",
    )


def _decision_is_available(decision: str, available: set[str] | None) -> bool:
    return available is None or decision in available


def _exact_scope_json(params: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    scope = {key: params[key] for key in keys if key in params}
    try:
        rendered = canonical_json(scope)
    except (TypeError, ValueError) as exc:
        raise _ScopeNotConveyable("scope is not canonical JSON") from exc
    if len(rendered) > _MAX_SCOPE_JSON_CHARS:
        raise _ScopeNotConveyable("scope is too large for an exact voice readback")
    # Sanitization must not alter or redact the exact request.  If it would,
    # refusing is safer than asking the owner to approve a lossy representation.
    safe = sanitize_untrusted_text(rendered, max_chars=_MAX_SCOPE_JSON_CHARS + 64)
    if safe != rendered:
        raise _ScopeNotConveyable("scope cannot be conveyed exactly and safely")
    return rendered


def _validate_optional_string(
    params: Mapping[str, Any],
    field: str,
    *,
    max_chars: int,
) -> None:
    if field not in params or params[field] is None:
        return
    value = params[field]
    if not isinstance(value, str) or len(value) > max_chars:
        raise _ScopeNotConveyable(f"{field} must be a bounded string or null")


def _validate_string_list(value: object, field: str) -> None:
    if not _is_string_list(value):
        raise _ScopeNotConveyable(f"{field} must be an array of strings")


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _workspace_ref(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).name[:200] or "workspace"


def _bounded_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:160]


def _dedupe_key(kind: str, params: Mapping[str, Any]) -> str:
    stable = canonical_json({"kind": kind, "params": params})
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
    return f"codex-approval:{kind}:{digest}"
