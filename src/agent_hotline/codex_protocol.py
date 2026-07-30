"""Small, version-tolerant types for the Codex app-server JSONL protocol.

The installed Codex 0.144.6 schemas intentionally leave most result payloads open
ended.  These types model the transport envelope without attempting to copy the
hundreds of generated protocol definitions into this project.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

type JSONPrimitive = str | int | float | bool | None
type JSONValue = JSONPrimitive | list[JSONValue] | dict[str, JSONValue]
type RequestId = str | int


class CodexAppServerError(RuntimeError):
    """Base error raised by the Codex app-server adapter."""


class CodexAppServerNotRunning(CodexAppServerError):
    """Raised when an operation requires a live, initialized app-server."""


class CodexAppServerConnectionError(CodexAppServerError):
    """Raised when the supervised app-server transport is lost."""


class CodexAppServerProtocolError(CodexAppServerError):
    """Raised when app-server sends a malformed protocol envelope."""


class UnsupportedCodexMethod(CodexAppServerError):
    """Raised when a caller attempts a method outside the adapter's safe surface."""


class CodexRequestTimeout(CodexAppServerError):
    """Raised when a correlated app-server request does not finish in time."""

    def __init__(self, method: str, timeout: float) -> None:
        self.method = method
        self.timeout = timeout
        super().__init__(f"Codex request {method!r} timed out after {timeout:g}s")


class CodexRequestError(CodexAppServerError):
    """A structured error returned by app-server for a client request."""

    def __init__(
        self,
        *,
        request_id: RequestId,
        code: int,
        message: str,
        data: JSONValue = None,
    ) -> None:
        self.request_id = request_id
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Codex request {request_id!r} failed ({code}): {message}")


class ServerRequestFailure(CodexAppServerError):
    """A deliberate JSON-RPC error response from a server-request handler."""

    def __init__(self, code: int, message: str, data: JSONValue = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(message)


class ThreadResolutionError(CodexAppServerError):
    """Base error for conservative thread selection."""


class ThreadNotFoundError(ThreadResolutionError):
    """No thread safely matched a human-provided reference."""


class AmbiguousThreadError(ThreadResolutionError):
    """More than one thread matched a human-provided reference."""

    def __init__(self, reference: str, candidates: tuple[ThreadCandidate, ...]) -> None:
        self.reference = reference
        self.candidates = candidates
        labels = ", ".join(candidate.label for candidate in candidates)
        super().__init__(f"Thread reference {reference!r} is ambiguous: {labels}")


class UnsafeWorkspaceError(CodexAppServerError):
    """A requested thread operation falls outside configured workspace roots."""


class ThreadStateError(CodexAppServerError):
    """A requested control operation is incompatible with current thread state."""


@dataclass(frozen=True, slots=True)
class CodexNotification:
    """A notification emitted by app-server."""

    method: str
    params: JSONValue
    received_monotonic: float


@dataclass(frozen=True, slots=True)
class CodexServerRequest:
    """A request initiated by app-server and addressed to this client."""

    request_id: RequestId
    method: str
    params: JSONValue


@runtime_checkable
class ServerRequestHandler(Protocol):
    """Pluggable handler for approval, elicitation, and other server requests."""

    async def handle(self, request: CodexServerRequest) -> JSONValue:
        """Return the JSON result, or raise :class:`ServerRequestFailure`."""


@dataclass(frozen=True, slots=True)
class CodexAppServerStatus:
    """Point-in-time health snapshot for watchdogs and status endpoints."""

    state: str
    running: bool
    initialized: bool
    pending_requests: int
    active_server_requests: int
    pid: int | None
    restart_count: int
    last_error: str | None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""

        return {
            "state": self.state,
            "running": self.running,
            "initialized": self.initialized,
            "pending_requests": self.pending_requests,
            "active_server_requests": self.active_server_requests,
            "pid": self.pid,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
        }


@dataclass(frozen=True, slots=True)
class ThreadCandidate:
    """Minimal thread identity used during conservative voice disambiguation."""

    thread_id: str
    name: str | None
    preview: str
    cwd: str
    status: str
    updated_at: int | None

    @property
    def label(self) -> str:
        return self.name or self.preview or self.thread_id


@dataclass(frozen=True, slots=True)
class ThreadControlResult:
    """Result of a safe high-level thread control operation."""

    action: str
    thread_id: str
    turn_id: str | None
    response: dict[str, JSONValue]


@dataclass(frozen=True, slots=True)
class ThreadWritePlan:
    """Server-derived, immutable precondition for one exact Codex instruction write."""

    thread_id: str
    cwd: str
    operation: str
    turn_id: str | None
    state_fingerprint: str
    instruction: str


def ensure_json_mapping(value: JSONValue, *, context: str) -> dict[str, JSONValue]:
    """Narrow an open protocol result to an object with a useful error."""

    if not isinstance(value, dict):
        raise CodexAppServerProtocolError(f"{context} returned a non-object result")
    return value


__all__ = [
    "AmbiguousThreadError",
    "CodexAppServerConnectionError",
    "CodexAppServerError",
    "CodexAppServerNotRunning",
    "CodexAppServerProtocolError",
    "CodexAppServerStatus",
    "CodexNotification",
    "CodexRequestError",
    "CodexRequestTimeout",
    "CodexServerRequest",
    "JSONValue",
    "RequestId",
    "ServerRequestFailure",
    "ServerRequestHandler",
    "ThreadCandidate",
    "ThreadControlResult",
    "ThreadNotFoundError",
    "ThreadResolutionError",
    "ThreadStateError",
    "UnsafeWorkspaceError",
    "UnsupportedCodexMethod",
    "ensure_json_mapping",
]
