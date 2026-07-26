"""Supervised, safe Codex app-server client for Agent Hotline.

Codex 0.144.6 speaks one JSON message per line over stdio.  Although the
transport follows JSON-RPC request/response semantics, the generated schema
does not require a ``jsonrpc`` member, so this client emits the exact compact
envelopes accepted by that version.

Only thread and turn control methods needed by Agent Hotline are exposed.  In
particular, this module deliberately has no command execution or shell-command
surface; urgent operational actions belong in separately registered runbooks.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import os
import shutil
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

from .codex_protocol import (
    AmbiguousThreadError,
    CodexAppServerConnectionError,
    CodexAppServerError,
    CodexAppServerNotRunning,
    CodexAppServerProtocolError,
    CodexAppServerStatus,
    CodexNotification,
    CodexRequestError,
    CodexRequestTimeout,
    CodexServerRequest,
    JSONValue,
    RequestId,
    ServerRequestFailure,
    ServerRequestHandler,
    ThreadCandidate,
    ThreadControlResult,
    ThreadNotFoundError,
    ThreadStateError,
    UnsafeWorkspaceError,
    UnsupportedCodexMethod,
    ensure_json_mapping,
)
from .security import sanitize_untrusted_text

logger = logging.getLogger(__name__)

_MAX_JSONL_BYTES = 8 * 1024 * 1024
_MAX_INSTRUCTION_CHARS = 100_000
_SUBSCRIPTION_STOP = object()
_VOICE_CANDIDATE_CACHE_LIMIT = 10
_DEFAULT_VOICE_CANDIDATE_CACHE_TTL_SECONDS = 15.0
_DEFAULT_VOICE_LIST_TIMEOUT_SECONDS = 25.0

# This is intentionally narrower than the full app-server protocol.  Do not add
# command/exec, process/spawn, thread/shellCommand, fs/*, or dynamic tool calls.
SAFE_CLIENT_METHODS = frozenset(
    {
        "thread/archive",
        "thread/list",
        "thread/read",
        "thread/resume",
        "thread/start",
        "turn/interrupt",
        "turn/start",
        "turn/steer",
    }
)


class _LifecycleState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    FAILED = "failed"


class _Reader(Protocol):
    async def readline(self) -> bytes: ...


class _Writer(Protocol):
    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class _Process(Protocol):
    stdin: _Writer | None
    stdout: _Reader | None
    stderr: _Reader | None
    pid: int
    returncode: int | None

    async def wait(self) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


ProcessFactory = Callable[..., Awaitable[_Process]]
HandlerCallable = Callable[[CodexServerRequest], JSONValue | Awaitable[JSONValue]]
HandlerLike = ServerRequestHandler | HandlerCallable


class NotificationSubscription:
    """Independent notification stream that does not consume other subscribers.

    Subscriptions are async iterators and async context managers.  An unbounded
    queue is used by default so a watchdog does not silently miss a lifecycle
    event.  When a positive ``max_queue`` is requested, the oldest event is
    dropped on overflow and ``dropped`` is incremented.
    """

    def __init__(
        self,
        owner: CodexAppServerClient,
        method: str | None,
        max_queue: int,
    ) -> None:
        self._owner = owner
        self.method = method
        self._queue: asyncio.Queue[CodexNotification | object] = asyncio.Queue(maxsize=max_queue)
        self._closed = False
        self.dropped = 0

    @property
    def closed(self) -> bool:
        return self._closed

    async def get(self) -> CodexNotification:
        """Wait for the next matching event.

        ``StopAsyncIteration`` is raised after the subscription is closed, which
        keeps ``get`` and async iteration behavior consistent.
        """

        item = await self._queue.get()
        if item is _SUBSCRIPTION_STOP:
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(_SUBSCRIPTION_STOP)
            raise StopAsyncIteration
        return cast(CodexNotification, item)

    def _put(self, notification: CodexNotification) -> None:
        if self._closed:
            return
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.dropped += 1
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(notification)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner._remove_subscription(self)
        if self._queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_SUBSCRIPTION_STOP)

    def __aiter__(self) -> NotificationSubscription:
        return self

    async def __anext__(self) -> CodexNotification:
        return await self.get()

    async def __aenter__(self) -> NotificationSubscription:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()


class CodexAppServerClient:
    """Own and supervise one native Codex app-server subprocess.

    The process is always launched with :func:`asyncio.create_subprocess_exec`;
    no shell, PowerShell, ``cmd.exe``, WSL, or user-supplied command arguments
    are involved.  On Windows, the resolver requires a real ``codex.exe`` and
    rejects npm ``.cmd``/``.ps1`` shims.
    """

    def __init__(
        self,
        *,
        codex_executable: str | os.PathLike[str] | None = None,
        process_cwd: str | os.PathLike[str] | None = None,
        client_name: str = "agent-hotline",
        client_title: str = "Agent Hotline",
        client_version: str = "0.1.0",
        experimental_api: bool = True,
        opt_out_notifications: Sequence[str] | None = None,
        request_timeout: float = 30.0,
        initialize_timeout: float = 15.0,
        shutdown_timeout: float = 5.0,
        server_request_handler: HandlerLike | None = None,
        _process_factory: ProcessFactory | None = None,
    ) -> None:
        if request_timeout <= 0 or initialize_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if not client_name or not client_version:
            raise ValueError("client_name and client_version are required")

        self._codex_executable = os.fspath(codex_executable) if codex_executable else None
        self._process_cwd = (
            Path(process_cwd).expanduser().resolve(strict=False) if process_cwd else None
        )
        self._client_name = client_name
        self._client_title = client_title
        self._client_version = client_version
        self._experimental_api = experimental_api
        self._opt_out_notifications = tuple(opt_out_notifications or ())
        self._request_timeout = request_timeout
        self._initialize_timeout = initialize_timeout
        self._shutdown_timeout = shutdown_timeout
        self._process_factory: ProcessFactory = (
            _process_factory
            if _process_factory is not None
            else cast(ProcessFactory, asyncio.create_subprocess_exec)
        )
        self._uses_native_factory = _process_factory is None

        self._state = _LifecycleState.STOPPED
        self._initialized = False
        self._process: _Process | None = None
        self._generation = 0
        self._restart_count = 0
        self._last_error: str | None = None
        self._next_request_id = 1
        self._initialize_result: dict[str, JSONValue] = {}

        self._lifecycle_lock = asyncio.Lock()
        self._writer_lock = asyncio.Lock()
        self._pending: dict[RequestId, asyncio.Future[JSONValue]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._waiter_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()

        self.notification_queue: asyncio.Queue[CodexNotification] = asyncio.Queue()
        self._subscriptions: set[NotificationSubscription] = set()
        self._default_server_request_handler = server_request_handler
        self._server_request_handlers: dict[str, HandlerLike] = {}
        self._stderr_tail: deque[str] = deque(maxlen=40)

    async def __aenter__(self) -> CodexAppServerClient:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    @property
    def is_running(self) -> bool:
        process = self._process
        return (
            self._state is _LifecycleState.RUNNING
            and self._initialized
            and process is not None
            and process.returncode is None
        )

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        """Recent app-server stderr lines, bounded and primarily for diagnostics."""

        return tuple(self._stderr_tail)

    async def status(self) -> CodexAppServerStatus:
        """Return a non-consuming health snapshot suitable for a watchdog."""

        process = self._process
        running = (
            self._state is _LifecycleState.RUNNING
            and self._initialized
            and process is not None
            and process.returncode is None
        )
        return CodexAppServerStatus(
            state=self._state.value,
            running=running,
            initialized=self._initialized,
            pending_requests=len(self._pending),
            active_server_requests=len(self._server_request_tasks),
            pid=process.pid if process is not None and process.returncode is None else None,
            restart_count=self._restart_count,
            last_error=self._last_error,
        )

    async def health(self) -> CodexAppServerStatus:
        """Alias for :meth:`status` used by health endpoints."""

        return await self.status()

    async def start(self) -> dict[str, JSONValue]:
        """Spawn app-server and complete ``initialize``/``initialized``."""

        async with self._lifecycle_lock:
            if self.is_running:
                return dict(self._initialize_result)
            if self._process is not None:
                await self._shutdown_locked(final=False)
            return await self._start_locked()

    async def restart(self) -> dict[str, JSONValue]:
        """Gracefully replace the child process without closing subscribers."""

        async with self._lifecycle_lock:
            await self._shutdown_locked(final=False)
            self._restart_count += 1
            return await self._start_locked()

    async def close(self) -> None:
        """Close stdin, wait briefly, then terminate/kill only if necessary."""

        async with self._lifecycle_lock:
            await self._shutdown_locked(final=True)

    async def _start_locked(self) -> dict[str, JSONValue]:
        self._state = _LifecycleState.STARTING
        self._initialized = False
        self._last_error = None
        try:
            executable = self._resolve_executable()
        except CodexAppServerConnectionError as exc:
            self._state = _LifecycleState.FAILED
            self._last_error = str(exc)
            raise
        spawn_kwargs: dict[str, Any] = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "limit": _MAX_JSONL_BYTES + 1,
        }
        if self._process_cwd is not None:
            spawn_kwargs["cwd"] = os.fspath(self._process_cwd)
        if os.name == "nt" and self._uses_native_factory:
            spawn_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            process = await self._process_factory(
                executable,
                "app-server",
                "--stdio",
                **spawn_kwargs,
            )
        except (OSError, RuntimeError) as exc:
            self._state = _LifecycleState.FAILED
            self._last_error = f"failed to start Codex app-server: {exc}"
            raise CodexAppServerConnectionError(self._last_error) from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
            self._state = _LifecycleState.FAILED
            self._last_error = "Codex app-server did not expose all stdio pipes"
            raise CodexAppServerConnectionError(self._last_error)

        self._process = process
        self._generation += 1
        generation = self._generation
        self._reader_task = self._background_task(
            self._reader_loop(process, generation), "codex-app-server-reader"
        )
        self._stderr_task = self._background_task(
            self._stderr_loop(process, generation), "codex-app-server-stderr"
        )
        self._waiter_task = self._background_task(
            self._waiter_loop(process, generation), "codex-app-server-waiter"
        )

        capabilities: dict[str, JSONValue] = {
            "experimentalApi": self._experimental_api,
        }
        if self._opt_out_notifications:
            capabilities["optOutNotificationMethods"] = list(self._opt_out_notifications)
        initialize_params: dict[str, JSONValue] = {
            "clientInfo": {
                "name": self._client_name,
                "title": self._client_title,
                "version": self._client_version,
            },
            "capabilities": capabilities,
        }

        try:
            result = await self._request_internal(
                "initialize",
                initialize_params,
                request_timeout=self._initialize_timeout,
                allow_starting=True,
            )
            initialize_result = ensure_json_mapping(result, context="initialize")
            await self._write_message({"method": "initialized"}, allow_starting=True)
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self._last_error = f"Codex app-server initialization failed: {exc}"
            await self._shutdown_locked(final=False)
            self._state = _LifecycleState.FAILED
            raise

        self._initialized = True
        self._state = _LifecycleState.RUNNING
        self._initialize_result = dict(initialize_result)
        return initialize_result

    async def _shutdown_locked(self, *, final: bool) -> None:
        process = self._process
        if process is None:
            self._initialized = False
            self._initialize_result = {}
            self._state = _LifecycleState.STOPPED
            self._fail_pending(CodexAppServerConnectionError("Codex app-server closed"))
            if final:
                await self._close_subscriptions()
            return

        self._state = _LifecycleState.CLOSING
        self._initialized = False
        self._generation += 1  # Make stale transport tasks harmless immediately.
        self._fail_pending(CodexAppServerConnectionError("Codex app-server closed"))

        current_task = asyncio.current_task()
        server_tasks = tuple(
            task for task in self._server_request_tasks if task is not current_task
        )
        for task in server_tasks:
            task.cancel()
        if server_tasks:
            await asyncio.gather(*server_tasks, return_exceptions=True)

        if process.stdin is not None:
            with contextlib.suppress(
                BrokenPipeError,
                ConnectionError,
                RuntimeError,
                TimeoutError,
            ):
                process.stdin.close()
                await asyncio.wait_for(process.stdin.wait_closed(), timeout=self._shutdown_timeout)

        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=self._shutdown_timeout)

        background = tuple(
            task
            for task in (self._reader_task, self._stderr_task, self._waiter_task)
            if task is not None and task is not asyncio.current_task()
        )
        for task in background:
            if not task.done():
                task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)

        self._process = None
        self._initialize_result = {}
        self._reader_task = None
        self._stderr_task = None
        self._waiter_task = None
        self._server_request_tasks.clear()
        self._state = _LifecycleState.STOPPED
        if final:
            await self._close_subscriptions()

    def _resolve_executable(self) -> str:
        candidate = self._codex_executable
        if not self._uses_native_factory:
            return candidate or ("codex.exe" if os.name == "nt" else "codex")

        if os.name != "nt":
            path = (
                shutil.which(candidate)
                if candidate and not Path(candidate).is_absolute()
                else candidate or shutil.which("codex")
            )
            if not path:
                raise CodexAppServerConnectionError(
                    "Could not find a native Codex executable on PATH"
                )
            return os.fspath(Path(path).expanduser().resolve(strict=False))

        wrapper_directories: list[Path] = []
        if candidate:
            requested = Path(candidate).expanduser()
            if requested.is_absolute():
                resolved = requested.resolve(strict=False)
                if resolved.suffix.casefold() != ".exe":
                    raise CodexAppServerConnectionError(
                        "Windows app-server requires native codex.exe; "
                        "script launchers are rejected"
                    )
                return os.fspath(resolved)
            located = shutil.which(candidate)
            if located:
                located_path = Path(located).resolve(strict=False)
                if located_path.suffix.casefold() == ".exe":
                    return os.fspath(located_path)
                wrapper_directories.append(located_path.parent)
        else:
            for launcher in ("codex.cmd", "codex.ps1", "codex"):
                located = shutil.which(launcher)
                if located:
                    wrapper_directories.append(Path(located).resolve(strict=False).parent)

        # npm's Windows launcher is a .cmd/.ps1 script, but the package contains
        # the real Rust codex.exe below it. Resolve that binary directly rather
        # than invoking the launcher through cmd.exe or PowerShell.
        for directory in dict.fromkeys(wrapper_directories):
            package_root = (
                directory / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai"
            )
            try:
                packaged_binaries = sorted(
                    package_root.glob("codex-win32-*/vendor/*/bin/codex.exe"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                continue
            if packaged_binaries:
                return os.fspath(packaged_binaries[0].resolve(strict=False))

        direct = shutil.which("codex.exe")
        if direct:
            return os.fspath(Path(direct).resolve(strict=False))
        raise CodexAppServerConnectionError("Could not find a native Codex executable on PATH")

    def _background_task(self, awaitable: Awaitable[None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(awaitable, name=name)
        task.add_done_callback(self._consume_background_exception)
        return task

    @staticmethod
    def _consume_background_exception(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.exception()

    async def _reader_loop(self, process: _Process, generation: int) -> None:
        assert process.stdout is not None
        try:
            while generation == self._generation:
                line = await process.stdout.readline()
                if not line:
                    await self._transport_lost(
                        generation,
                        CodexAppServerConnectionError(
                            "Codex app-server stdout closed unexpectedly"
                        ),
                    )
                    return
                if len(line) > _MAX_JSONL_BYTES:
                    self._record_protocol_error("discarded oversized app-server message")
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._record_protocol_error(f"discarded malformed JSONL message: {exc}")
                    continue
                if not isinstance(message, dict):
                    self._record_protocol_error("discarded non-object protocol message")
                    continue
                self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._transport_lost(
                generation,
                CodexAppServerConnectionError(f"Codex app-server reader failed: {exc}"),
            )

    async def _stderr_loop(self, process: _Process, generation: int) -> None:
        assert process.stderr is not None
        try:
            while generation == self._generation:
                line = await process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_tail.append(text[:4000])
                    logger.debug("codex app-server: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Codex app-server stderr reader failed: %s", exc)

    async def _waiter_loop(self, process: _Process, generation: int) -> None:
        try:
            returncode = await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._transport_lost(
                generation,
                CodexAppServerConnectionError(f"waiting for Codex app-server failed: {exc}"),
            )
            return
        await self._transport_lost(
            generation,
            CodexAppServerConnectionError(
                f"Codex app-server exited unexpectedly with code {returncode}"
            ),
        )

    async def _transport_lost(self, generation: int, error: CodexAppServerConnectionError) -> None:
        if generation != self._generation:
            return
        if self._state in {_LifecycleState.CLOSING, _LifecycleState.STOPPED}:
            return
        self._initialized = False
        self._state = _LifecycleState.FAILED
        self._last_error = str(error)
        self._fail_pending(error)

    def _record_protocol_error(self, message: str) -> None:
        self._last_error = message
        logger.warning("%s", message)

    def _fail_pending(self, error: BaseException) -> None:
        pending = tuple(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(error)

    def _dispatch_message(self, message: dict[str, Any]) -> None:
        has_id = "id" in message
        method = message.get("method")
        if has_id and method is None and ("result" in message or "error" in message):
            self._dispatch_response(message)
            return
        if isinstance(method, str) and has_id:
            request_id = message["id"]
            if not self._valid_request_id(request_id):
                self._record_protocol_error("discarded server request with invalid id")
                return
            request = CodexServerRequest(
                request_id=cast(RequestId, request_id),
                method=method,
                params=cast(JSONValue, message.get("params")),
            )
            task = self._background_task(
                self._handle_server_request(request),
                f"codex-server-request-{request_id}",
            )
            self._server_request_tasks.add(task)
            task.add_done_callback(self._server_request_tasks.discard)
            return
        if isinstance(method, str) and not has_id:
            self._publish_notification(
                CodexNotification(
                    method=method,
                    params=cast(JSONValue, message.get("params")),
                    received_monotonic=time.monotonic(),
                )
            )
            return
        self._record_protocol_error("discarded unrecognized app-server envelope")

    def _dispatch_response(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not self._valid_request_id(request_id):
            self._record_protocol_error("discarded response with invalid id")
            return
        future = self._pending.pop(cast(RequestId, request_id), None)
        if future is None or future.done():
            logger.debug("Ignoring response for unknown request id %r", request_id)
            return

        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict):
                future.set_exception(
                    CodexAppServerProtocolError("app-server returned a malformed error")
                )
                return
            code = error.get("code")
            text = error.get("message")
            if isinstance(code, bool) or not isinstance(code, int) or not isinstance(text, str):
                future.set_exception(
                    CodexAppServerProtocolError("app-server returned a malformed error")
                )
                return
            future.set_exception(
                CodexRequestError(
                    request_id=cast(RequestId, request_id),
                    code=code,
                    message=text,
                    data=cast(JSONValue, error.get("data")),
                )
            )
            return
        future.set_result(cast(JSONValue, message.get("result")))

    @staticmethod
    def _valid_request_id(value: object) -> bool:
        return isinstance(value, (str, int)) and not isinstance(value, bool)

    def _publish_notification(self, notification: CodexNotification) -> None:
        self.notification_queue.put_nowait(notification)
        for subscription in tuple(self._subscriptions):
            if subscription.method is None or subscription.method == notification.method:
                subscription._put(notification)

    def subscribe(
        self, method: str | None = None, *, max_queue: int = 0
    ) -> NotificationSubscription:
        """Create an independent fan-out stream for one method or all methods."""

        if method is not None and not method:
            raise ValueError("method must be non-empty or None")
        if max_queue < 0:
            raise ValueError("max_queue cannot be negative")
        subscription = NotificationSubscription(self, method, max_queue)
        self._subscriptions.add(subscription)
        return subscription

    def _remove_subscription(self, subscription: NotificationSubscription) -> None:
        self._subscriptions.discard(subscription)

    async def _close_subscriptions(self) -> None:
        subscriptions = tuple(self._subscriptions)
        for subscription in subscriptions:
            await subscription.close()

    def set_server_request_handler(self, handler: HandlerLike | None) -> None:
        """Set the fallback handler for server-initiated requests."""

        self._default_server_request_handler = handler

    def register_server_request_handler(self, method: str, handler: HandlerLike) -> None:
        """Register a method-specific approval/elicitation handler."""

        if not method:
            raise ValueError("method is required")
        self._server_request_handlers[method] = handler

    def remove_server_request_handler(self, method: str) -> None:
        self._server_request_handlers.pop(method, None)

    async def _handle_server_request(self, request: CodexServerRequest) -> None:
        handler = self._server_request_handlers.get(
            request.method, self._default_server_request_handler
        )
        if handler is None:
            await self._send_server_error(
                request.request_id,
                ServerRequestFailure(-32601, f"No handler registered for {request.method!r}"),
            )
            return

        try:
            if isinstance(handler, ServerRequestHandler):
                result = await handler.handle(request)
            else:
                pending_result = handler(request)
                result = (
                    await pending_result if inspect.isawaitable(pending_result) else pending_result
                )
            await self._write_message(
                {"id": request.request_id, "result": result},
                allow_starting=True,
            )
        except asyncio.CancelledError:
            raise
        except ServerRequestFailure as exc:
            await self._send_server_error(request.request_id, exc)
        except Exception:
            logger.exception("Codex server-request handler failed for %s", request.method)
            await self._send_server_error(
                request.request_id,
                ServerRequestFailure(-32603, "Agent Hotline handler failed"),
            )

    async def _send_server_error(self, request_id: RequestId, error: ServerRequestFailure) -> None:
        payload: dict[str, JSONValue] = {
            "code": error.code,
            "message": error.message,
        }
        if error.data is not None:
            payload["data"] = error.data
        with contextlib.suppress(CodexAppServerConnectionError, CodexAppServerNotRunning):
            await self._write_message(
                {"id": request_id, "error": payload},
                allow_starting=True,
            )

    async def request(
        self,
        method: str,
        params: Mapping[str, JSONValue] | None = None,
        *,
        request_timeout: float | None = None,
    ) -> JSONValue:
        """Send a correlated request from the explicitly safe method allowlist."""

        if method not in SAFE_CLIENT_METHODS:
            raise UnsupportedCodexMethod(f"Codex method {method!r} is not exposed by Agent Hotline")
        return await self._request_internal(
            method,
            dict(params or {}),
            request_timeout=request_timeout,
            allow_starting=False,
        )

    async def _request_internal(
        self,
        method: str,
        params: Mapping[str, JSONValue],
        *,
        request_timeout: float | None,
        allow_starting: bool,
    ) -> JSONValue:
        if allow_starting:
            usable = self._state in {
                _LifecycleState.STARTING,
                _LifecycleState.RUNNING,
            }
        else:
            usable = self.is_running
        if not usable:
            raise CodexAppServerNotRunning("Codex app-server is not initialized")

        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[JSONValue] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write_message(
                {
                    "id": request_id,
                    "method": method,
                    "params": dict(params),
                },
                allow_starting=allow_starting,
            )
        except BaseException:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                # A transport failure may have completed every pending future
                # before the write path raises. Retrieve this exception so it
                # does not become an unobserved-future warning.
                future.exception()
            elif not future.done():
                future.cancel()
            raise

        effective_timeout = self._request_timeout if request_timeout is None else request_timeout
        if effective_timeout <= 0:
            self._pending.pop(request_id, None)
            future.cancel()
            raise ValueError("timeout must be positive")
        try:
            return await asyncio.wait_for(future, timeout=effective_timeout)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise CodexRequestTimeout(method, effective_timeout) from exc
        except asyncio.CancelledError:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            raise

    async def _write_message(self, message: Mapping[str, Any], *, allow_starting: bool) -> None:
        if allow_starting:
            usable = self._state in {
                _LifecycleState.STARTING,
                _LifecycleState.RUNNING,
            }
        else:
            usable = self.is_running
        process = self._process
        if not usable or process is None or process.stdin is None:
            raise CodexAppServerNotRunning("Codex app-server transport is unavailable")
        try:
            encoded = (
                json.dumps(
                    message,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError) as exc:
            raise CodexAppServerProtocolError(f"request is not valid JSON: {exc}") from exc
        if len(encoded) > _MAX_JSONL_BYTES:
            raise CodexAppServerProtocolError("request exceeds JSONL size limit")

        try:
            async with self._writer_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError) as exc:
            error = CodexAppServerConnectionError(f"failed writing to Codex app-server: {exc}")
            await self._transport_lost(self._generation, error)
            raise error from exc

    async def thread_list(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        archived: bool | None = None,
        cwd: str | os.PathLike[str] | Sequence[str | os.PathLike[str]] | None = None,
        search_term: str | None = None,
        sort_key: str | None = None,
        sort_direction: str | None = None,
        source_kinds: Sequence[str] | None = None,
        parent_thread_id: str | None = None,
        ancestor_thread_id: str | None = None,
        request_timeout: float | None = None,
    ) -> dict[str, JSONValue]:
        if limit is not None and not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if sort_key not in {None, "created_at", "updated_at", "recency_at"}:
            raise ValueError("unsupported thread sort key")
        if sort_direction not in {None, "asc", "desc"}:
            raise ValueError("sort_direction must be 'asc' or 'desc'")
        if parent_thread_id is not None and ancestor_thread_id is not None:
            raise ValueError("parent and ancestor filters are mutually exclusive")

        params: dict[str, JSONValue] = {}
        _set_if_not_none(params, "cursor", cursor)
        _set_if_not_none(params, "limit", limit)
        _set_if_not_none(params, "archived", archived)
        if cwd is not None:
            if isinstance(cwd, (str, os.PathLike)):
                params["cwd"] = os.fspath(cwd)
            else:
                params["cwd"] = [os.fspath(path) for path in cwd]
        _set_if_not_none(params, "searchTerm", search_term)
        _set_if_not_none(params, "sortKey", sort_key)
        _set_if_not_none(params, "sortDirection", sort_direction)
        if source_kinds is not None:
            params["sourceKinds"] = list(source_kinds)
        _set_if_not_none(params, "parentThreadId", parent_thread_id)
        _set_if_not_none(params, "ancestorThreadId", ancestor_thread_id)
        result = await self.request(
            "thread/list",
            params,
            request_timeout=request_timeout,
        )
        return ensure_json_mapping(result, context="thread/list")

    async def thread_read(
        self, thread_id: str, *, include_turns: bool = True
    ) -> dict[str, JSONValue]:
        _require_identifier(thread_id, "thread_id")
        result = await self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        return ensure_json_mapping(result, context="thread/read")

    async def thread_start(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        sandbox: Mapping[str, JSONValue] | str | None = None,
        permissions: str | None = None,
        service_name: str | None = "agent-hotline",
        service_tier: str | None = None,
        personality: str | None = None,
        developer_instructions: str | None = None,
        ephemeral: bool | None = None,
    ) -> dict[str, JSONValue]:
        params: dict[str, JSONValue] = {}
        if cwd is not None:
            params["cwd"] = os.fspath(cwd)
        _set_if_not_none(params, "model", model)
        _set_if_not_none(params, "modelProvider", model_provider)
        _set_if_not_none(params, "approvalPolicy", approval_policy)
        if sandbox is not None:
            params["sandbox"] = dict(sandbox) if isinstance(sandbox, Mapping) else sandbox
        _set_if_not_none(params, "permissions", permissions)
        _set_if_not_none(params, "serviceName", service_name)
        _set_if_not_none(params, "serviceTier", service_tier)
        _set_if_not_none(params, "personality", personality)
        _set_if_not_none(params, "developerInstructions", developer_instructions)
        _set_if_not_none(params, "ephemeral", ephemeral)
        result = await self.request("thread/start", params)
        return ensure_json_mapping(result, context="thread/start")

    async def thread_resume(
        self,
        thread_id: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        model: str | None = None,
        model_provider: str | None = None,
        approval_policy: str | None = None,
        permissions: str | None = None,
        sandbox: Mapping[str, JSONValue] | str | None = None,
        exclude_turns: bool | None = None,
    ) -> dict[str, JSONValue]:
        _require_identifier(thread_id, "thread_id")
        params: dict[str, JSONValue] = {"threadId": thread_id}
        if cwd is not None:
            params["cwd"] = os.fspath(cwd)
        _set_if_not_none(params, "model", model)
        _set_if_not_none(params, "modelProvider", model_provider)
        _set_if_not_none(params, "approvalPolicy", approval_policy)
        _set_if_not_none(params, "permissions", permissions)
        if sandbox is not None:
            params["sandbox"] = dict(sandbox) if isinstance(sandbox, Mapping) else sandbox
        _set_if_not_none(params, "excludeTurns", exclude_turns)
        result = await self.request("thread/resume", params)
        return ensure_json_mapping(result, context="thread/resume")

    async def thread_archive(self, thread_id: str) -> dict[str, JSONValue]:
        _require_identifier(thread_id, "thread_id")
        result = await self.request("thread/archive", {"threadId": thread_id})
        return ensure_json_mapping(result, context="thread/archive")

    async def turn_start(
        self,
        thread_id: str,
        input: str | Sequence[Mapping[str, JSONValue]],
        *,
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        permissions: str | None = None,
        sandbox_policy: Mapping[str, JSONValue] | str | None = None,
        service_tier: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> dict[str, JSONValue]:
        _require_identifier(thread_id, "thread_id")
        params: dict[str, JSONValue] = {
            "threadId": thread_id,
            "input": _normalize_input(input),
        }
        _set_if_not_none(params, "model", model)
        _set_if_not_none(params, "effort", effort)
        _set_if_not_none(params, "approvalPolicy", approval_policy)
        _set_if_not_none(params, "permissions", permissions)
        if sandbox_policy is not None:
            params["sandboxPolicy"] = (
                dict(sandbox_policy) if isinstance(sandbox_policy, Mapping) else sandbox_policy
            )
        _set_if_not_none(params, "serviceTier", service_tier)
        if cwd is not None:
            params["cwd"] = os.fspath(cwd)
        result = await self.request("turn/start", params)
        return ensure_json_mapping(result, context="turn/start")

    async def turn_steer(
        self,
        thread_id: str,
        expected_turn_id: str,
        input: str | Sequence[Mapping[str, JSONValue]],
    ) -> dict[str, JSONValue]:
        _require_identifier(thread_id, "thread_id")
        _require_identifier(expected_turn_id, "expected_turn_id")
        result = await self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": expected_turn_id,
                "input": _normalize_input(input),
            },
        )
        return ensure_json_mapping(result, context="turn/steer")

    async def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, JSONValue]:
        _require_identifier(thread_id, "thread_id")
        _require_identifier(turn_id, "turn_id")
        result = await self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )
        return ensure_json_mapping(result, context="turn/interrupt")


class SafeThreadController:
    """Conservative, workspace-bounded controls for voice-originated requests.

    The helper resolves human-friendly references without silently selecting
    among multiple candidates, binds all mutations to a concrete thread ID, and
    only sends natural-language instructions through Codex turns.  It never
    executes a shell command.
    """

    def __init__(
        self,
        client: CodexAppServerClient,
        *,
        workspace_roots: Sequence[str | os.PathLike[str]],
        max_instruction_chars: int = 12_000,
        voice_candidate_cache_ttl_seconds: float = (_DEFAULT_VOICE_CANDIDATE_CACHE_TTL_SECONDS),
        voice_list_timeout_seconds: float = _DEFAULT_VOICE_LIST_TIMEOUT_SECONDS,
    ) -> None:
        if not workspace_roots:
            raise ValueError("at least one workspace root is required")
        if max_instruction_chars <= 0:
            raise ValueError("max_instruction_chars must be positive")
        if voice_candidate_cache_ttl_seconds <= 0 or voice_list_timeout_seconds <= 0:
            raise ValueError("voice candidate cache timing must be positive")
        self.client = client
        self.workspace_roots = tuple(
            Path(root).expanduser().resolve(strict=False) for root in workspace_roots
        )
        self.max_instruction_chars = max_instruction_chars
        self.voice_candidate_cache_ttl_seconds = voice_candidate_cache_ttl_seconds
        self.voice_list_timeout_seconds = voice_list_timeout_seconds
        self._voice_candidate_cache: tuple[ThreadCandidate, ...] = ()
        self._voice_candidate_cache_expires_at = 0.0
        self._voice_candidate_cache_epoch = 0
        self._voice_candidate_cache_lock = asyncio.Lock()
        self._voice_candidate_refresh_task: (
            asyncio.Task[tuple[ThreadCandidate, ...] | None] | None
        ) = None

    async def list_candidates(
        self, *, limit: int = 100, archived: bool = False
    ) -> tuple[ThreadCandidate, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not archived and limit <= _VOICE_CANDIDATE_CACHE_LIMIT:
            candidates = await self._voice_candidates()
            return candidates[:limit]
        return await self._list_candidates_uncached(
            limit=limit,
            archived=archived,
            request_timeout=self.voice_list_timeout_seconds,
        )

    async def prewarm_voice_candidates(
        self,
        *,
        timeout_seconds: float = _DEFAULT_VOICE_LIST_TIMEOUT_SECONDS,
    ) -> bool:
        """Fill the bounded voice cache without making startup depend on Codex."""

        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_seconds + 0.1):
                await self._voice_candidates(
                    force_refresh=True,
                    request_timeout=timeout_seconds,
                )
        except Exception:
            logger.warning(
                "codex_thread_list_prewarm_failed duration_ms=%.1f",
                (time.monotonic() - started) * 1000,
            )
            return False
        logger.info(
            "codex_thread_list_prewarm_completed duration_ms=%.1f",
            (time.monotonic() - started) * 1000,
        )
        return True

    def invalidate_voice_candidate_cache(self) -> None:
        """Invalidate cached discovery after a successful thread mutation."""

        self._voice_candidate_cache_epoch += 1
        self._voice_candidate_cache = ()
        self._voice_candidate_cache_expires_at = 0.0

    async def _voice_candidates(
        self,
        *,
        force_refresh: bool = False,
        request_timeout: float | None = None,
    ) -> tuple[ThreadCandidate, ...]:
        for _attempt in range(2):
            now = time.monotonic()
            if not force_refresh and now < self._voice_candidate_cache_expires_at:
                return self._voice_candidate_cache
            async with self._voice_candidate_cache_lock:
                now = time.monotonic()
                if not force_refresh and now < self._voice_candidate_cache_expires_at:
                    return self._voice_candidate_cache
                refresh = self._voice_candidate_refresh_task
                if refresh is None:
                    refresh = asyncio.create_task(
                        self._refresh_voice_candidates(
                            cache_epoch=self._voice_candidate_cache_epoch,
                            request_timeout=(
                                self.voice_list_timeout_seconds
                                if request_timeout is None
                                else request_timeout
                            ),
                        ),
                        name="agent-hotline-voice-thread-candidates",
                    )
                    refresh.add_done_callback(_consume_task_exception)
                    self._voice_candidate_refresh_task = refresh
            candidates = await asyncio.shield(refresh)
            if candidates is not None:
                return candidates
            force_refresh = True
        raise CodexAppServerError("voice thread candidates changed during refresh")

    async def _refresh_voice_candidates(
        self,
        *,
        cache_epoch: int,
        request_timeout: float,
    ) -> tuple[ThreadCandidate, ...] | None:
        current_task = asyncio.current_task()
        try:
            candidates = await self._list_candidates_uncached(
                limit=_VOICE_CANDIDATE_CACHE_LIMIT,
                archived=False,
                request_timeout=request_timeout,
            )
            cached = tuple(_sanitize_voice_candidate(candidate) for candidate in candidates)
            async with self._voice_candidate_cache_lock:
                if cache_epoch != self._voice_candidate_cache_epoch:
                    return None
                self._voice_candidate_cache = cached
                self._voice_candidate_cache_expires_at = (
                    time.monotonic() + self.voice_candidate_cache_ttl_seconds
                )
                return cached
        finally:
            async with self._voice_candidate_cache_lock:
                if self._voice_candidate_refresh_task is current_task:
                    self._voice_candidate_refresh_task = None

    async def _list_candidates_uncached(
        self,
        *,
        limit: int,
        archived: bool,
        request_timeout: float | None,
    ) -> tuple[ThreadCandidate, ...]:
        started = time.monotonic()
        try:
            response = await self.client.thread_list(
                limit=limit,
                archived=archived,
                sort_key="updated_at",
                sort_direction="desc",
                request_timeout=request_timeout,
            )
        finally:
            logger.info(
                "codex_thread_list duration_ms=%.1f",
                (time.monotonic() - started) * 1000,
            )
        raw_threads = response.get("data", [])
        if not isinstance(raw_threads, list):
            raise CodexAppServerProtocolError("thread/list data is not an array")
        candidates: list[ThreadCandidate] = []
        for raw in raw_threads:
            if not isinstance(raw, dict):
                continue
            candidate = self._candidate_from_thread(raw)
            if candidate is not None and self._workspace_allowed(candidate.cwd):
                candidates.append(candidate)
        return tuple(candidates)

    async def resolve_thread(self, reference: str) -> ThreadCandidate:
        """Resolve by ID/name/preview, raising on zero or multiple matches."""

        needle = reference.strip()
        if not needle:
            raise ThreadNotFoundError("thread reference is empty")
        folded = needle.casefold()
        candidates = await self.list_candidates()

        exact_id = [candidate for candidate in candidates if candidate.thread_id == needle]
        if len(exact_id) == 1:
            return exact_id[0]

        exact_named = [
            candidate
            for candidate in candidates
            if candidate.name is not None and candidate.name.casefold() == folded
        ]
        if len(exact_named) == 1:
            return exact_named[0]
        if len(exact_named) > 1:
            raise AmbiguousThreadError(needle, tuple(exact_named[:3]))

        exact_preview = [
            candidate for candidate in candidates if candidate.preview.casefold() == folded
        ]
        if len(exact_preview) == 1:
            return exact_preview[0]
        if len(exact_preview) > 1:
            raise AmbiguousThreadError(needle, tuple(exact_preview[:3]))

        partial = [
            candidate
            for candidate in candidates
            if any(
                folded in value.casefold()
                for value in (
                    candidate.thread_id,
                    candidate.name or "",
                    candidate.preview,
                    candidate.cwd,
                )
            )
        ]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            raise AmbiguousThreadError(needle, tuple(partial[:3]))
        raise ThreadNotFoundError(f"No allowed thread matches {needle!r}")

    async def inspect_thread(self, reference: str) -> dict[str, JSONValue]:
        candidate = await self.resolve_thread(reference)
        response = await self.client.thread_read(candidate.thread_id, include_turns=True)
        self._assert_response_workspace(response)
        return response

    async def spawn_root(
        self,
        *,
        task: str,
        cwd: str | os.PathLike[str],
        model: str | None = None,
        effort: str | None = None,
        approval_policy: str | None = None,
        permissions: str | None = None,
    ) -> ThreadControlResult:
        instruction = self._validate_instruction(task)
        normalized_cwd = self._require_allowed_cwd(cwd)
        started = await self.client.thread_start(
            cwd=normalized_cwd,
            model=model,
            approval_policy=approval_policy,
            permissions=permissions,
        )
        self.invalidate_voice_candidate_cache()
        thread = started.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise CodexAppServerProtocolError("thread/start omitted thread.id")
        thread_id = cast(str, thread["id"])
        turn = await self.client.turn_start(
            thread_id,
            instruction,
            effort=effort,
        )
        turn_id = _turn_id_from_response(turn)
        return ThreadControlResult(
            action="spawned",
            thread_id=thread_id,
            turn_id=turn_id,
            response=turn,
        )

    async def send_instruction(self, reference: str, instruction: str) -> ThreadControlResult:
        """Start on an idle thread or steer its one known active turn."""

        text = self._validate_instruction(instruction)
        candidate = await self.resolve_thread(reference)
        response = await self.client.thread_read(candidate.thread_id, include_turns=True)
        self._assert_response_workspace(response)
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise CodexAppServerProtocolError("thread/read omitted thread")
        status = _thread_status(thread)
        active_turn_ids = _active_turn_ids(thread)
        if status == "notLoaded":
            response = await self.client.thread_resume(candidate.thread_id)
            self.invalidate_voice_candidate_cache()
            self._assert_response_workspace(response)
            thread = response.get("thread")
            if not isinstance(thread, dict):
                raise CodexAppServerProtocolError("thread/resume omitted thread")
            status = _thread_status(thread)
            active_turn_ids = _active_turn_ids(thread)
        if status == "active":
            if len(active_turn_ids) != 1:
                raise ThreadStateError("active thread does not expose exactly one in-progress turn")
            result = await self.client.turn_steer(
                candidate.thread_id,
                active_turn_ids[0],
                text,
            )
            self.invalidate_voice_candidate_cache()
            return ThreadControlResult(
                action="steered",
                thread_id=candidate.thread_id,
                turn_id=cast(str | None, result.get("turnId")),
                response=result,
            )
        if status != "idle":
            raise ThreadStateError(
                f"thread {candidate.thread_id} cannot accept an instruction in {status!r}"
            )
        result = await self.client.turn_start(candidate.thread_id, text)
        self.invalidate_voice_candidate_cache()
        return ThreadControlResult(
            action="started",
            thread_id=candidate.thread_id,
            turn_id=_turn_id_from_response(result),
            response=result,
        )

    async def interrupt(self, reference: str, *, turn_id: str | None = None) -> ThreadControlResult:
        """Interrupt one exact active turn; never guess among active turns."""

        candidate = await self.resolve_thread(reference)
        response = await self.client.thread_read(candidate.thread_id, include_turns=True)
        self._assert_response_workspace(response)
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise CodexAppServerProtocolError("thread/read omitted thread")
        active_turn_ids = _active_turn_ids(thread)
        if turn_id is not None:
            _require_identifier(turn_id, "turn_id")
            if active_turn_ids and turn_id not in active_turn_ids:
                raise ThreadStateError("confirmed turn is no longer the active turn")
            selected_turn = turn_id
        elif len(active_turn_ids) == 1:
            selected_turn = active_turn_ids[0]
        elif not active_turn_ids:
            raise ThreadStateError("thread has no in-progress turn to interrupt")
        else:
            raise ThreadStateError("thread exposes multiple in-progress turns")

        result = await self.client.turn_interrupt(candidate.thread_id, selected_turn)
        self.invalidate_voice_candidate_cache()
        return ThreadControlResult(
            action="interrupted",
            thread_id=candidate.thread_id,
            turn_id=selected_turn,
            response=result,
        )

    async def pause(self, reference: str, *, turn_id: str | None = None) -> ThreadControlResult:
        """App-server pause semantics are an exact turn interruption."""

        return await self.interrupt(reference, turn_id=turn_id)

    async def archive(self, reference: str, *, confirmed_thread_id: str) -> ThreadControlResult:
        """Archive only when readback confirmation is bound to the resolved ID."""

        candidate = await self.resolve_thread(reference)
        if candidate.thread_id != confirmed_thread_id:
            raise ThreadStateError("archive confirmation does not match resolved thread")
        result = await self.client.thread_archive(candidate.thread_id)
        self.invalidate_voice_candidate_cache()
        return ThreadControlResult(
            action="archived",
            thread_id=candidate.thread_id,
            turn_id=None,
            response=result,
        )

    def _candidate_from_thread(self, thread: Mapping[str, Any]) -> ThreadCandidate | None:
        thread_id = thread.get("id")
        cwd = thread.get("cwd")
        if not isinstance(thread_id, str) or not isinstance(cwd, str):
            return None
        name = thread.get("name")
        preview = thread.get("preview")
        updated_at = thread.get("updatedAt")
        return ThreadCandidate(
            thread_id=thread_id,
            name=name if isinstance(name, str) else None,
            preview=preview if isinstance(preview, str) else "",
            cwd=cwd,
            status=_thread_status(thread),
            updated_at=updated_at if isinstance(updated_at, int) else None,
        )

    def _workspace_allowed(self, cwd: str) -> bool:
        candidate = Path(cwd).expanduser().resolve(strict=False)
        return any(candidate.is_relative_to(root) for root in self.workspace_roots)

    def _require_allowed_cwd(self, cwd: str | os.PathLike[str]) -> Path:
        candidate = Path(cwd).expanduser().resolve(strict=False)
        if not any(candidate.is_relative_to(root) for root in self.workspace_roots):
            raise UnsafeWorkspaceError(f"workspace {candidate} is outside configured roots")
        return candidate

    def _assert_response_workspace(self, response: Mapping[str, JSONValue]) -> None:
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("cwd"), str):
            raise CodexAppServerProtocolError("thread response omitted thread.cwd")
        if not self._workspace_allowed(cast(str, thread["cwd"])):
            raise UnsafeWorkspaceError("thread moved outside configured workspace roots")

    def _validate_instruction(self, instruction: str) -> str:
        text = instruction.strip()
        if not text:
            raise ValueError("instruction is required")
        if len(text) > self.max_instruction_chars:
            raise ValueError("instruction exceeds configured length limit")
        return text


def _set_if_not_none(target: dict[str, JSONValue], key: str, value: JSONValue) -> None:
    if value is not None:
        target[key] = value


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Mark detached refresh failures observed without changing waiter behavior."""

    if not task.cancelled():
        task.exception()


def _require_identifier(value: str, name: str) -> None:
    if not value or len(value) > 500:
        raise ValueError(f"{name} must be between 1 and 500 characters")


def _normalize_input(
    value: str | Sequence[Mapping[str, JSONValue]],
) -> list[JSONValue]:
    if isinstance(value, str):
        if not value.strip():
            raise ValueError("turn input cannot be empty")
        if len(value) > _MAX_INSTRUCTION_CHARS:
            raise ValueError("turn input exceeds length limit")
        return [{"type": "text", "text": value}]
    normalized: list[JSONValue] = []
    allowed_types = {"text", "image", "localImage", "skill", "mention"}
    for item in value:
        copied = dict(item)
        item_type = copied.get("type")
        if item_type not in allowed_types:
            raise ValueError(f"unsupported Codex input type: {item_type!r}")
        normalized.append(cast(JSONValue, copied))
    if not normalized:
        raise ValueError("turn input cannot be empty")
    return normalized


def _thread_status(thread: Mapping[str, Any]) -> str:
    status = thread.get("status")
    if isinstance(status, dict) and isinstance(status.get("type"), str):
        return cast(str, status["type"])
    if isinstance(status, str):  # Defensive compatibility with older versions.
        return status
    return "unknown"


def _sanitize_voice_candidate(candidate: ThreadCandidate) -> ThreadCandidate:
    """Cache only bounded display text while retaining exact opaque identifiers."""

    return ThreadCandidate(
        thread_id=candidate.thread_id,
        name=(
            sanitize_untrusted_text(candidate.name, max_chars=300)
            if candidate.name is not None
            else None
        ),
        preview=sanitize_untrusted_text(candidate.preview, max_chars=500),
        cwd=candidate.cwd,
        status=sanitize_untrusted_text(candidate.status, max_chars=100),
        updated_at=candidate.updated_at,
    )


def _active_turn_ids(thread: Mapping[str, Any]) -> list[str]:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return []
    active: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            continue
        status = turn.get("status")
        status_type = status.get("type") if isinstance(status, dict) else status
        if status_type == "inProgress":
            active.append(cast(str, turn["id"]))
    return active


def _turn_id_from_response(response: Mapping[str, JSONValue]) -> str | None:
    turn = response.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return cast(str, turn["id"])
    turn_id = response.get("turnId")
    return turn_id if isinstance(turn_id, str) else None


__all__ = [
    "SAFE_CLIENT_METHODS",
    "CodexAppServerClient",
    "NotificationSubscription",
    "SafeThreadController",
]
