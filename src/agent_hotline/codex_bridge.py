"""Codex App Server callbacks routed through the durable voice coordinator."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .codex_protocol import CodexServerRequest, JSONValue, ServerRequestFailure
from .contracts import ContactHumanRequest, ContextPacket, EvidenceReference
from .coordinator import HotlineCoordinator
from .security import canonical_json, sanitize_untrusted_text

COMMAND_APPROVAL = "item/commandExecution/requestApproval"
FILE_APPROVAL = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL = "item/permissions/requestApproval"

logger = logging.getLogger(__name__)


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
            if request.method == PERMISSIONS_APPROVAL:
                return {"scope": "turn", "permissions": {}}
            return {"decision": "decline"}

    async def _command_approval(self, params: dict[str, Any]) -> JSONValue:
        thread_id = _bounded_identifier(params.get("threadId"))
        turn_id = _bounded_identifier(params.get("turnId"))
        reason = _safe_text(params.get("reason"), "Codex requests command execution.")
        command = _command_display(params.get("command"))
        network = params.get("networkApprovalContext")
        if isinstance(network, Mapping):
            host = _safe_text(network.get("host"), "an unspecified host")
            protocol = _safe_text(network.get("protocol"), "network")
            action_summary = f"Allow {protocol} access to {host} for this turn."
        else:
            action_summary = f"Run this command once: {command}"
        result = await self.coordinator.contact_human(
            ContactHumanRequest(
                source="codex_app_server",
                kind="approval",
                severity="high",
                summary="Codex is paused on a command or network approval.",
                question=f"Approve once or decline? {action_summary}",
                context=ContextPacket(
                    thread_id=thread_id,
                    workspace_ref=_workspace_ref(params.get("cwd")),
                    task_summary=reason,
                    pending_action_summary=action_summary,
                    evidence=[
                        EvidenceReference(
                            kind="other",
                            ref=turn_id or thread_id or "codex-approval",
                            summary=action_summary,
                        )
                    ],
                ),
                dedupe_key=_dedupe_key("command", params),
                timeout_seconds=600,
            )
        )
        return {
            "decision": _approval_decision(
                result.outcome,
                result.status,
                result.identity_verified,
            )
        }

    async def _file_approval(self, params: dict[str, Any]) -> JSONValue:
        thread_id = _bounded_identifier(params.get("threadId"))
        turn_id = _bounded_identifier(params.get("turnId"))
        reason = _safe_text(params.get("reason"), "Codex requests a file change.")
        root = _workspace_ref(params.get("grantRoot"))
        scope = f" under workspace {root}" if root else ""
        result = await self.coordinator.contact_human(
            ContactHumanRequest(
                source="codex_app_server",
                kind="approval",
                severity="high",
                summary="Codex is paused on a file-change approval.",
                question=f"Approve this file change once or decline? {reason}{scope}",
                context=ContextPacket(
                    thread_id=thread_id,
                    workspace_ref=root,
                    task_summary=reason,
                    pending_action_summary=f"Apply the pending Codex file change{scope}.",
                    evidence=[
                        EvidenceReference(
                            kind="thread",
                            ref=turn_id or thread_id or "codex-file-approval",
                            summary=reason,
                        )
                    ],
                ),
                dedupe_key=_dedupe_key("file", params),
                timeout_seconds=600,
            )
        )
        return {
            "decision": _approval_decision(
                result.outcome,
                result.status,
                result.identity_verified,
            )
        }

    async def _permissions_approval(self, params: dict[str, Any]) -> JSONValue:
        thread_id = _bounded_identifier(params.get("threadId"))
        reason = _safe_text(params.get("reason"), "Codex requests additional permissions.")
        permissions = params.get("permissions")
        if not isinstance(permissions, dict):
            return {"scope": "turn", "permissions": {}}
        summary = _safe_text(str(permissions), "additional permissions")
        result = await self.coordinator.contact_human(
            ContactHumanRequest(
                source="codex_app_server",
                kind="approval",
                severity="high",
                summary="Codex is paused on a scoped permission request.",
                question=f"Grant only the requested permissions for this turn? {summary}",
                context=ContextPacket(
                    thread_id=thread_id,
                    workspace_ref=_workspace_ref(params.get("cwd")),
                    task_summary=reason,
                    pending_action_summary=summary,
                ),
                dedupe_key=_dedupe_key("permissions", params),
                timeout_seconds=600,
            )
        )
        if result.status == "resolved" and result.outcome == "approve" and result.identity_verified:
            # App Server itself ignores anything beyond the requested subset.
            return {"scope": "turn", "permissions": permissions}
        return {"scope": "turn", "permissions": {}}


def register_voice_callbacks(client: Any, handler: VoiceApprovalHandler) -> None:
    """Register only protocol methods with deterministic response schemas."""

    for method in (COMMAND_APPROVAL, FILE_APPROVAL, PERMISSIONS_APPROVAL):
        client.register_server_request_handler(method, handler)


def _approval_decision(outcome: str, status: str, identity_verified: bool) -> str:
    if status == "resolved" and outcome == "approve" and identity_verified:
        return "accept"
    # Timeout, no answer, busy, model/tool failure, deferral, and ambiguous
    # instructions all decline. They never broaden to session approval.
    return "decline"


def _command_display(value: object) -> str:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return sanitize_untrusted_text(" ".join(value), max_chars=1200)
    if isinstance(value, str):
        return sanitize_untrusted_text(value, max_chars=1200)
    return "the command details supplied by Codex"


def _workspace_ref(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return Path(value).name[:200] or "workspace"


def _safe_text(value: object, default: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return default
    return sanitize_untrusted_text(value, max_chars=1200)


def _bounded_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:160]


def _dedupe_key(kind: str, params: Mapping[str, Any]) -> str:
    stable = canonical_json({"kind": kind, "params": params})
    digest = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:32]
    return f"codex-approval:{kind}:{digest}"
