"""Independent failure detector for Codex/Claude and infrastructure events."""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .contracts import ContactHumanRequest, ContextPacket, EvidenceReference


@dataclass(slots=True)
class WatchdogPolicy:
    repeated_error_threshold: int = 2
    cooldown_seconds: float = 300.0
    call_on_provider_error: bool = True
    call_on_turn_failure: bool = True
    call_on_process_exit: bool = True


@dataclass(slots=True)
class AgentFailureWatchdog:
    """Convert independent runtime signals into deduplicated escalations.

    This component does not require a model turn to choose an MCP tool. The daemon can
    subscribe it directly to App Server notifications or feed it process/infrastructure
    monitor events.
    """

    policy: WatchdogPolicy = field(default_factory=WatchdogPolicy)
    _error_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _last_emitted: dict[str, float] = field(default_factory=dict)

    def evaluate_codex_notification(
        self, method: str, params: Mapping[str, Any]
    ) -> ContactHumanRequest | None:
        thread_id = _first_string(params, "threadId", "thread_id") or _nested_id(params, "thread")
        turn_id = _first_string(params, "turnId", "turn_id") or _nested_id(params, "turn")
        key = f"codex:{thread_id or 'unknown'}:{turn_id or 'unknown'}"

        if method == "turn/completed":
            status = _nested_status(params, "turn") or _first_string(params, "status")
            if status and status.lower() in {"failed", "error", "cancelled"}:
                if not self.policy.call_on_turn_failure or not self._may_emit(key):
                    return None
                error = _extract_error(params) or f"Codex turn ended with status {status}."
                return self._provider_failure_request(
                    source="codex_app_server",
                    thread_id=thread_id,
                    turn_id=turn_id,
                    error=error,
                    dedupe_key=f"{key}:{status}",
                )

        if method in {"error", "turn/error", "codex/event/error"}:
            error = _extract_error(params) or "Codex emitted an unspecified runtime error."
            signature = _error_signature(error)
            count_key = f"{key}:{signature}"
            self._error_counts[count_key] += 1
            if (
                self._error_counts[count_key] < self.policy.repeated_error_threshold
                or not self.policy.call_on_provider_error
                or not self._may_emit(count_key)
            ):
                return None
            return self._provider_failure_request(
                source="codex_app_server",
                thread_id=thread_id,
                turn_id=turn_id,
                error=error,
                dedupe_key=count_key,
            )
        return None

    def evaluate_process_exit(
        self,
        *,
        agent: str,
        return_code: int | None,
        thread_id: str | None = None,
        stderr_tail: str | None = None,
    ) -> ContactHumanRequest | None:
        if not self.policy.call_on_process_exit or return_code in (0, None):
            return None
        key = f"process:{agent}:{thread_id or 'unknown'}:{return_code}"
        if not self._may_emit(key):
            return None
        error = f"{agent} exited unexpectedly with code {return_code}."
        if stderr_tail:
            error = f"{error} {_bounded(stderr_tail, 1200)}"
        return self._provider_failure_request(
            source="watchdog",
            thread_id=thread_id,
            turn_id=None,
            error=error,
            dedupe_key=key,
        )

    def evaluate_infrastructure_alert(
        self,
        *,
        alert_name: str,
        summary: str,
        severity: str,
        resource_ref: str,
        evidence: Mapping[str, Any] | None = None,
    ) -> ContactHumanRequest | None:
        normalized_severity = severity.lower()
        if normalized_severity not in {"high", "critical"}:
            return None
        key = f"infra:{alert_name}:{resource_ref}:{_error_signature(summary)}"
        if not self._may_emit(key):
            return None
        evidence_summary = (
            json.dumps(evidence, sort_keys=True, default=str)[:1500] if evidence else summary
        )
        return ContactHumanRequest(
            source="watchdog",
            kind="incident",
            severity=normalized_severity,
            summary=_bounded(summary, 1000),
            question=(
                "Should I run the registered recovery action, keep the system paused, or defer?"
            ),
            dedupe_key=key[:200],
            context=ContextPacket(
                task_summary=f"Infrastructure alert: {alert_name}",
                last_error=_bounded(summary, 3000),
                evidence=[
                    EvidenceReference(
                        kind="metric",
                        ref=_bounded(resource_ref, 500),
                        summary=_bounded(evidence_summary, 1000),
                    )
                ],
            ),
        )

    def _provider_failure_request(
        self,
        *,
        source: str,
        thread_id: str | None,
        turn_id: str | None,
        error: str,
        dedupe_key: str,
    ) -> ContactHumanRequest:
        return ContactHumanRequest(
            source=source,  # type: ignore[arg-type]
            kind="provider_failure",
            severity="high",
            summary="The coding agent or provider failed and could not continue normally.",
            question="Retry later, leave the task paused, or move it to another authorized agent?",
            dedupe_key=_bounded(dedupe_key, 200),
            context=ContextPacket(
                thread_id=thread_id,
                task_summary=f"Failed turn {turn_id}" if turn_id else "Agent process failure",
                last_error=_bounded(error, 3000),
                evidence=[
                    EvidenceReference(
                        kind="error",
                        ref=turn_id or thread_id or "runtime",
                        summary=_bounded(error, 1000),
                    )
                ],
            ),
        )

    def _may_emit(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last_emitted.get(key)
        if last is not None and now - last < self.policy.cooldown_seconds:
            return False
        self._last_emitted[key] = now
        return True


def _nested_id(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if isinstance(value, Mapping):
        identifier = value.get("id")
        return identifier if isinstance(identifier, str) else None
    return None


def _nested_status(params: Mapping[str, Any], key: str) -> str | None:
    value = params.get(key)
    if isinstance(value, Mapping):
        status = value.get("status")
        if isinstance(status, Mapping):
            status = status.get("type")
        return status if isinstance(status, str) else None
    return None


def _first_string(params: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str):
            return value
    return None


def _extract_error(params: Mapping[str, Any]) -> str | None:
    for key in ("error", "message", "failureReason", "failure_reason"):
        value = params.get(key)
        if isinstance(value, str):
            return _bounded(value, 3000)
        if isinstance(value, Mapping):
            nested = value.get("message") or value.get("detail")
            if isinstance(nested, str):
                return _bounded(nested, 3000)
    turn = params.get("turn")
    if isinstance(turn, Mapping):
        error = turn.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            return _bounded(error["message"], 3000)
        if isinstance(error, str):
            return _bounded(error, 3000)
    return None


def _error_signature(value: str) -> str:
    normalized = " ".join(value.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _bounded(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value[:limit]
