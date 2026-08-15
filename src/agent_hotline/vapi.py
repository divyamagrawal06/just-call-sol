"""Authenticated Vapi webhook and Codex function-tool adapter."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import Mapping

from pydantic import JsonValue

from .contracts import (
    BeginInboundSessionRequest,
    PrepareActionRequest,
    ThreadInspectRequest,
    ThreadListRequest,
)
from .coordinator import HotlineCoordinator
from .models import ProviderWebhookPayload, WebhookStatus
from .settings import Settings
from .storage import ConflictError, SQLiteStore

logger = logging.getLogger(__name__)

_MAX_TOOL_CALLS = 8
_MAX_RESULT_CHARS = 16_000
_TOOL_ALIASES = {
    "list_codex_tasks": "list_threads",
    "inspect_codex_task": "inspect_thread",
    "instruct_codex_task": "thread.instruct",
    "interrupt_codex_task": "thread.interrupt",
    "spawn_codex_task": "thread.spawn_root",
    "archive_codex_task": "thread.archive",
}
_MANAGE_ACTIONS = {
    "instruct": "thread.instruct",
    "interrupt": "thread.interrupt",
    "spawn": "thread.spawn_root",
    "archive": "thread.archive",
}
_FAILED_END_REASONS = (
    "error",
    "failed",
    "pipeline-error",
    "provider-error",
    "sip-error",
)


class VapiProtocolError(ValueError):
    """Raised when a Vapi webhook cannot be safely correlated."""


class VapiAdapter:
    """Translate Vapi server messages into durable Hotline coordinator calls."""

    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        coordinator: HotlineCoordinator,
    ) -> None:
        self.settings = settings
        self.store = store
        self.coordinator = coordinator
        self._tool_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task[object]] = set()

    async def handle(self, payload: object) -> dict[str, JsonValue]:
        if not isinstance(payload, Mapping):
            return {"accepted": False, "results": []}
        message = payload.get("message")
        if not isinstance(message, Mapping):
            return {"accepted": False, "results": []}
        message_type = message.get("type")
        try:
            if message_type == "tool-calls":
                return await self._handle_tool_calls(message)
            if message_type == "end-of-call-report" or (
                message_type == "status-update" and message.get("status") == "ended"
            ):
                return await self._handle_call_end(message)
        except VapiProtocolError as exc:
            logger.warning("Rejected invalid Vapi message type=%s", message_type)
            if message_type == "tool-calls":
                return {
                    "results": [
                        {"toolCallId": tool_call_id, "error": _safe_tool_error(exc)}
                        for tool_call_id, _name, _arguments in _extract_tool_calls(message)
                    ]
                }
            return {"accepted": False}
        return {"accepted": True}

    async def _handle_tool_calls(
        self,
        message: Mapping[str, object],
    ) -> dict[str, JsonValue]:
        raw_calls = _extract_tool_calls(message)
        if not raw_calls:
            return {"results": []}
        if len(raw_calls) > _MAX_TOOL_CALLS:
            raw_calls = raw_calls[:_MAX_TOOL_CALLS]

        call_id, caller_number = self._validate_call(message)
        interaction_id = _interaction_id(call_id)
        session = await self.coordinator.begin_inbound_session(
            BeginInboundSessionRequest(
                caller_phone_number=caller_number,
                interaction_id=interaction_id,
            ),
            provider="vapi",
        )
        if not session.accepted or session.event_id is None:
            reason = _single_line(session.message_to_user)
            return {
                "results": [
                    {"toolCallId": tool_call_id, "error": reason}
                    for tool_call_id, _name, _arguments in raw_calls
                ]
            }

        results: list[dict[str, JsonValue]] = []
        for tool_call_id, name, arguments in raw_calls:
            results.append(
                await self._run_tool(
                    call_id=call_id,
                    event_id=session.event_id,
                    tool_call_id=tool_call_id,
                    name=name,
                    arguments=arguments,
                )
            )
        return {"results": results}

    async def _run_tool(
        self,
        *,
        call_id: str,
        event_id: str,
        tool_call_id: str,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        key = (call_id, tool_call_id)
        lock = self._tool_locks.setdefault(key, asyncio.Lock())
        async with lock:
            encoded_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            arguments_hash = hashlib.sha256(encoded_arguments.encode("utf-8")).hexdigest()
            existing = await self.store.get_realtime_tool_receipt(call_id, tool_call_id)
            if existing is not None:
                stored_name, stored_hash, stored_output = existing
                if stored_name != name or not hmac.compare_digest(
                    stored_hash,
                    arguments_hash,
                ):
                    return {
                        "toolCallId": tool_call_id,
                        "error": "This tool-call ID was reused with different content.",
                    }
                return _stored_result(tool_call_id, stored_output)

            try:
                value = await self._dispatch(event_id, name, arguments)
                result_body: dict[str, JsonValue] = {"result": _compact_result(value)}
            except Exception as exc:
                logger.warning(
                    "Vapi tool failed call_id=%s tool=%s error_type=%s",
                    call_id,
                    name,
                    type(exc).__name__,
                )
                result_body = {"error": _safe_tool_error(exc)}

            stored_output = json.dumps(
                result_body,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            try:
                await self.store.record_realtime_tool_receipt(
                    call_id,
                    tool_call_id,
                    tool_name=name,
                    arguments_hash=arguments_hash,
                    output_json=stored_output,
                )
            except ConflictError:
                existing = await self.store.get_realtime_tool_receipt(call_id, tool_call_id)
                if existing is None:
                    raise
                stored_name, stored_hash, stored_output = existing
                if stored_name != name or not hmac.compare_digest(
                    stored_hash,
                    arguments_hash,
                ):
                    return {
                        "toolCallId": tool_call_id,
                        "error": "This tool-call ID was reused with different content.",
                    }
            return _stored_result(tool_call_id, stored_output)

    async def _dispatch(
        self,
        event_id: str,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> object:
        resolved = _TOOL_ALIASES.get(name, name)
        if resolved == "list_threads":
            return await self.coordinator.list_threads(
                ThreadListRequest(
                    event_id=event_id,
                    query=_optional_text(arguments.get("query")),
                    limit=_bounded_limit(arguments.get("limit")),
                )
            )
        if resolved == "inspect_thread":
            return await self.coordinator.inspect_thread(
                ThreadInspectRequest(
                    event_id=event_id,
                    reference=_required_text(arguments, "reference", maximum=200),
                )
            )

        if resolved == "manage_codex_task":
            requested_action = _required_text(arguments, "action", maximum=40)
            resolved = _MANAGE_ACTIONS.get(requested_action, requested_action)

        if resolved in {
            "thread.instruct",
            "thread.interrupt",
            "thread.spawn_root",
            "thread.archive",
        }:
            parameters = _action_parameters(resolved, arguments)
            request = PrepareActionRequest(
                event_id=event_id,
                action_type=resolved,
                parameters=parameters,
                workspace_ref=_optional_text(arguments.get("workspace_ref")),
                thread_id=_optional_text(arguments.get("thread_id")),
            )
            if (
                resolved in {"thread.spawn_root", "thread.instruct"}
                and self.settings.hotline_demo_auto_execute_actions
            ):
                return self._queue_demo_action(request)
            response = await self.coordinator.prepare_action(request)
            return response.model_dump(mode="json")

        if resolved == "prepare_action":
            action_type = _required_text(arguments, "action_type", maximum=80)
            raw_parameters = arguments.get("parameters")
            if not isinstance(raw_parameters, dict):
                raise VapiProtocolError("parameters must be an object")
            response = await self.coordinator.prepare_action(
                PrepareActionRequest(
                    event_id=event_id,
                    action_type=action_type,
                    parameters=raw_parameters,
                    workspace_ref=_optional_text(arguments.get("workspace_ref")),
                    thread_id=_optional_text(arguments.get("thread_id")),
                )
            )
            return response.model_dump(mode="json")
        raise VapiProtocolError(f"unsupported tool {name!r}")

    def _queue_demo_action(self, request: PrepareActionRequest) -> dict[str, JsonValue]:
        """Return before slow Codex start/resume operations complete."""

        task = asyncio.create_task(
            self.coordinator.prepare_action(request),
            name=f"agent-hotline-vapi-action-{request.event_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)
        if request.action_type == "thread.spawn_root":
            message = (
                "Codex task request queued. The live task terminal will show its task "
                "ID when Codex accepts it."
            )
            action = "spawn_queued"
        else:
            message = (
                "Codex instruction queued. The live task terminal will show when the "
                "existing task accepts it."
            )
            action = "instruction_queued"
        return {
            "accepted": True,
            "queued": True,
            "executed": False,
            "message_to_user": message,
            "result": {"action": action, "status": "queued"},
        }

    def _background_task_done(self, task: asyncio.Task[object]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning(
                "Queued Vapi action failed error_type=%s",
                type(exc).__name__,
            )

    async def _handle_call_end(
        self,
        message: Mapping[str, object],
    ) -> dict[str, JsonValue]:
        call_id = _call_id(message)
        interaction_id = _interaction_id(call_id)
        session = await self.store.get_session_by_interaction(interaction_id)
        if session is None or session.event_id is None:
            return {"accepted": True, "created": False}
        call = message.get("call")
        assert isinstance(call, Mapping)
        self._validate_terminal_resources(message, call)
        ended_reason = (
            _optional_text(message.get("endedReason"))
            or _optional_text(call.get("endedReason"))
            or "completed"
        )
        status = _webhook_status(ended_reason)
        normalized = ProviderWebhookPayload(
            webhook_id=f"vapi:end:{call_id}",
            attempt_id=interaction_id,
            interaction_id=interaction_id,
            status=status,
            provider="vapi",
            duration_seconds=_optional_duration(
                message.get("durationSeconds", call.get("durationSeconds"))
            ),
            failure_reason=(ended_reason if status is WebhookStatus.FAILED else None),
            metadata={"event_id": session.event_id, "ended_reason": ended_reason},
        )
        return await self.coordinator.reconcile_provider_completion(normalized)

    def _validate_terminal_resources(
        self,
        message: Mapping[str, object],
        call: Mapping[str, object],
    ) -> None:
        """Validate any resource expansions present in a terminal payload.

        Vapi's documented end-of-call example includes only the call ID and
        status. The bearer-authenticated ID is already bound to a durable Vapi
        session, so omitted expansions are accepted while conflicts fail closed.
        """

        expected_assistant = self.settings.vapi_assistant_id
        expected_phone_number = self.settings.vapi_phone_number_id
        if expected_assistant is None or expected_phone_number is None:
            raise VapiProtocolError("Vapi resource binding is not configured")
        assistant_id = _resource_id(message, call, "assistant")
        phone_number_id = _resource_id(message, call, "phoneNumber")
        if assistant_id is not None and not hmac.compare_digest(
            expected_assistant,
            assistant_id,
        ):
            raise VapiProtocolError("Vapi assistant does not match Better Call Sol")
        if phone_number_id is not None and not hmac.compare_digest(
            expected_phone_number,
            phone_number_id,
        ):
            raise VapiProtocolError("Vapi phone number is not authorized")

    def _validate_call(self, message: Mapping[str, object]) -> tuple[str, str]:
        call_id = _call_id(message)
        call = message.get("call")
        assert isinstance(call, Mapping)
        caller_number = _customer_number(message, call)
        if caller_number is None:
            raise VapiProtocolError("Vapi call has no customer phone number")

        assistant_id = _resource_id(message, call, "assistant")
        phone_number_id = _resource_id(message, call, "phoneNumber")
        expected_assistant = self.settings.vapi_assistant_id
        expected_phone_number = self.settings.vapi_phone_number_id
        if expected_assistant is None or expected_phone_number is None:
            raise VapiProtocolError("Vapi resource binding is not configured")
        if assistant_id is None or not hmac.compare_digest(expected_assistant, assistant_id):
            raise VapiProtocolError("Vapi assistant does not match Better Call Sol")
        if phone_number_id is None or not hmac.compare_digest(
            expected_phone_number,
            phone_number_id,
        ):
            raise VapiProtocolError("Vapi phone number is not authorized")
        return call_id, caller_number


def _extract_tool_calls(
    message: Mapping[str, object],
) -> list[tuple[str, str, dict[str, JsonValue]]]:
    rows = message.get("toolCallList")
    if not isinstance(rows, list):
        wrappers = message.get("toolWithToolCallList")
        rows = []
        if isinstance(wrappers, list):
            for wrapper in wrappers:
                if isinstance(wrapper, Mapping) and isinstance(
                    wrapper.get("toolCall"),
                    Mapping,
                ):
                    rows.append(wrapper["toolCall"])

    result: list[tuple[str, str, dict[str, JsonValue]]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        function = row.get("function")
        function = function if isinstance(function, Mapping) else {}
        tool_call_id = row.get("id")
        name = row.get("name") or function.get("name")
        raw_arguments = (
            row.get("arguments")
            if row.get("arguments") is not None
            else row.get("parameters")
        )
        if raw_arguments is None:
            raw_arguments = function.get("arguments") or function.get("parameters")
        if isinstance(raw_arguments, str):
            try:
                raw_arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                raw_arguments = None
        if (
            not isinstance(tool_call_id, str)
            or not 1 <= len(tool_call_id) <= 300
            or not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or not isinstance(raw_arguments, dict)
        ):
            continue
        result.append((tool_call_id, name, raw_arguments))
    return result


def _call_id(message: Mapping[str, object]) -> str:
    call = message.get("call")
    if not isinstance(call, Mapping):
        raise VapiProtocolError("Vapi message has no call object")
    call_id = call.get("id")
    if not isinstance(call_id, str) or not 3 <= len(call_id) <= 150:
        raise VapiProtocolError("Vapi call ID is invalid")
    return call_id


def _interaction_id(call_id: str) -> str:
    return f"vapi:{call_id}"


def _customer_number(
    message: Mapping[str, object],
    call: Mapping[str, object],
) -> str | None:
    for candidate in (message.get("customer"), call.get("customer")):
        if isinstance(candidate, Mapping):
            number = candidate.get("number")
            if isinstance(number, str) and number.strip():
                return number.strip()
    direct = call.get("customerNumber")
    return direct.strip() if isinstance(direct, str) and direct.strip() else None


def _resource_id(
    message: Mapping[str, object],
    call: Mapping[str, object],
    resource: str,
) -> str | None:
    direct = call.get(f"{resource}Id")
    if isinstance(direct, str) and direct:
        return direct
    for candidate in (message.get(resource), call.get(resource)):
        if isinstance(candidate, Mapping):
            identifier = candidate.get("id")
            if isinstance(identifier, str) and identifier:
                return identifier
    return None


def _action_parameters(
    action_type: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    if action_type == "thread.instruct":
        return {
            "reference": _required_text(arguments, "reference", maximum=200),
            "instruction": _required_text(arguments, "instruction", maximum=2000),
        }
    if action_type == "thread.interrupt":
        result: dict[str, JsonValue] = {
            "reference": _required_text(arguments, "reference", maximum=200)
        }
        turn_id = _optional_text(arguments.get("turn_id"))
        if turn_id is not None:
            result["turn_id"] = turn_id
        return result
    if action_type == "thread.spawn_root":
        return {
            "task": _required_text(arguments, "task", maximum=2000),
            "cwd": _optional_text(arguments.get("cwd")) or ".",
        }
    return {
        "reference": _required_text(arguments, "reference", maximum=200),
        "confirmed_thread_id": _required_text(
            arguments,
            "confirmed_thread_id",
            maximum=200,
        ),
    }


def _required_text(
    arguments: Mapping[str, object],
    key: str,
    *,
    maximum: int,
) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise VapiProtocolError(f"{key} must be bounded text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _bounded_limit(value: object) -> int:
    if isinstance(value, bool):
        return 10
    if isinstance(value, int):
        return min(25, max(1, value))
    return 10


def _compact_result(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    encoded = _single_line(encoded)
    if len(encoded) > _MAX_RESULT_CHARS:
        # JSON escaping can nearly double a preview. Reserve enough space for
        # the wrapper so the bounded tool result remains bounded after encoding.
        preview = encoded[: (_MAX_RESULT_CHARS - 80) // 2]
        compact = json.dumps(
            {"preview": preview, "truncated": True},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return compact[:_MAX_RESULT_CHARS]
    return encoded


def _stored_result(tool_call_id: str, stored_output: str) -> dict[str, JsonValue]:
    try:
        body = json.loads(stored_output)
    except json.JSONDecodeError:
        body = {"error": "Stored tool result is invalid."}
    if not isinstance(body, dict):
        body = {"error": "Stored tool result is invalid."}
    result: dict[str, JsonValue] = {"toolCallId": tool_call_id}
    if isinstance(body.get("result"), str):
        result["result"] = body["result"]
    else:
        error = body.get("error")
        result["error"] = error if isinstance(error, str) else "Tool call failed."
    return result


def _safe_tool_error(exc: Exception) -> str:
    if isinstance(exc, (ValueError, PermissionError, RuntimeError)):
        detail = str(exc).strip()
        if detail:
            return _single_line(detail)[:500]
    return f"Tool call failed ({type(exc).__name__})."


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _optional_duration(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(86_400.0, max(0.0, float(value)))


def _webhook_status(ended_reason: str) -> WebhookStatus:
    normalized = ended_reason.casefold()
    if "no-answer" in normalized or "did-not-answer" in normalized:
        return WebhookStatus.NO_ANSWER
    if "busy" in normalized:
        return WebhookStatus.BUSY
    if any(marker in normalized for marker in _FAILED_END_REASONS):
        return WebhookStatus.FAILED
    return WebhookStatus.COMPLETED


__all__ = ["VapiAdapter", "VapiProtocolError"]
