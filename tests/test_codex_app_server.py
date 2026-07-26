from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_hotline.codex_app_server import (
    SAFE_CLIENT_METHODS,
    CodexAppServerClient,
    SafeThreadController,
)
from agent_hotline.codex_protocol import (
    AmbiguousThreadError,
    CodexAppServerConnectionError,
    CodexRequestError,
    CodexRequestTimeout,
    CodexServerRequest,
    ServerRequestFailure,
    ThreadNotFoundError,
    ThreadStateError,
    UnsafeWorkspaceError,
    UnsupportedCodexMethod,
)

NO_RESPONSE = object()


class FakeReader:
    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def readline(self) -> bytes:
        value = await self._lines.get()
        return b"" if value is None else value

    def feed_json(self, message: dict[str, Any]) -> None:
        self._lines.put_nowait(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")

    def feed_raw(self, line: bytes) -> None:
        self._lines.put_nowait(line)

    def close(self) -> None:
        self._lines.put_nowait(None)


class FakeWriter:
    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        on_close: Callable[[], None],
    ) -> None:
        self._on_message = on_message
        self._on_close = on_close
        self._buffer = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise BrokenPipeError
        self._buffer.extend(data)
        while b"\n" in self._buffer:
            raw, _, remainder = self._buffer.partition(b"\n")
            self._buffer[:] = remainder
            self._on_message(json.loads(raw))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._on_close()

    async def wait_closed(self) -> None:
        await asyncio.sleep(0)


class FakeProcess:
    _next_pid = 2000

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        *,
        exit_on_stdin_close: bool = True,
    ) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.returncode: int | None = None
        self.stdout = FakeReader()
        self.stderr = FakeReader()
        self._exit_event = asyncio.Event()
        self._exit_on_stdin_close = exit_on_stdin_close
        self.stdin = FakeWriter(on_message, self._stdin_closed)
        self.terminate_calls = 0
        self.kill_calls = 0

    def _stdin_closed(self) -> None:
        if self._exit_on_stdin_close:
            self.exit(0)

    def exit(self, returncode: int) -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        self.stdout.close()
        self.stderr.close()
        self._exit_event.set()

    async def wait(self) -> int:
        await self._exit_event.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.exit(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        self.exit(-9)


ResponseHandler = Callable[[dict[str, Any]], Any]


class FakeCodexServer:
    def __init__(self, *, exit_on_stdin_close: bool = True) -> None:
        self.messages: list[dict[str, Any]] = []
        self.handlers: dict[str, ResponseHandler] = {}
        self.process = FakeProcess(
            self._receive,
            exit_on_stdin_close=exit_on_stdin_close,
        )
        self.initialized = False

    def _receive(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        method = message.get("method")
        if method == "initialize":
            self.respond_result(
                message["id"],
                {
                    "codexHome": "C:\\fake-codex-home",
                    "platformFamily": "windows",
                    "platformOs": "windows",
                    "userAgent": "codex-cli/0.144.6",
                },
            )
            return
        if method == "initialized":
            self.initialized = True
            return
        handler = self.handlers.get(method)
        if handler is None:
            return
        try:
            result = handler(message)
        except ServerRequestFailure as exc:
            self.respond_error(message["id"], exc.code, exc.message, exc.data)
            return
        if result is not NO_RESPONSE:
            self.respond_result(message["id"], result)

    def respond_result(self, request_id: int | str, result: Any) -> None:
        self.process.stdout.feed_json({"id": request_id, "result": result})

    def respond_error(
        self,
        request_id: int | str,
        code: int,
        message: str,
        data: Any = None,
    ) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.process.stdout.feed_json({"id": request_id, "error": error})

    def notify(self, method: str, params: Any) -> None:
        self.process.stdout.feed_json({"method": method, "params": params})

    def request(self, request_id: int | str, method: str, params: Any) -> None:
        self.process.stdout.feed_json({"id": request_id, "method": method, "params": params})


class FakeProcessFactory:
    def __init__(self, *servers: FakeCodexServer) -> None:
        self.servers = list(servers)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        if not self.servers:
            raise RuntimeError("no fake server remains")
        return self.servers.pop(0).process


async def wait_until(predicate: Callable[[], bool]) -> None:
    pulse = asyncio.Event()
    async with asyncio.timeout(1):
        while not predicate():
            asyncio.get_running_loop().call_soon(pulse.set)
            await pulse.wait()
            pulse.clear()


def method_messages(server: FakeCodexServer, method: str) -> list[dict[str, Any]]:
    return [message for message in server.messages if message.get("method") == method]


@pytest.mark.asyncio
async def test_start_performs_exact_handshake_and_reports_health() -> None:
    server = FakeCodexServer()
    factory = FakeProcessFactory(server)
    client = CodexAppServerClient(
        codex_executable="C:\\fake\\codex.exe",
        client_version="9.8.7",
        _process_factory=factory,
    )

    result = await client.start()

    assert result["platformOs"] == "windows"
    assert factory.calls[0][0] == (
        "C:\\fake\\codex.exe",
        "app-server",
        "--stdio",
    )
    assert factory.calls[0][1]["limit"] > 64 * 1024
    initialize, initialized = server.messages[:2]
    assert initialize == {
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "agent-hotline",
                "title": "Agent Hotline",
                "version": "9.8.7",
            },
            "capabilities": {"experimentalApi": True},
        },
    }
    assert initialized == {"method": "initialized"}
    assert "jsonrpc" not in initialize
    assert server.initialized is True

    status = await client.status()
    assert status.running is True
    assert status.initialized is True
    assert status.pending_requests == 0
    assert status.pid == server.process.pid
    assert (await client.health()).as_dict()["state"] == "running"
    assert await client.start() == result
    assert len(factory.calls) == 1
    assert "shell" not in factory.calls[0][1]

    await client.close()
    closed = await client.status()
    assert closed.running is False
    assert closed.state == "stopped"


@pytest.mark.skipif(os.name != "nt", reason="Windows native resolver")
def test_windows_resolver_bypasses_npm_command_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "codex.cmd").write_text("@echo off\r\n", encoding="utf-8")
    native = (
        tmp_path
        / "node_modules"
        / "@openai"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "bin"
        / "codex.exe"
    )
    native.parent.mkdir(parents=True)
    native.touch()
    monkeypatch.setenv("PATH", str(tmp_path))
    client = CodexAppServerClient()

    assert Path(client._resolve_executable()) == native.resolve()


@pytest.mark.asyncio
async def test_correlates_out_of_order_responses_and_tracks_pending_requests() -> None:
    server = FakeCodexServer()
    server.handlers["thread/read"] = lambda _message: NO_RESPONSE
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()

    first = asyncio.create_task(client.thread_read("thread-a"))
    second = asyncio.create_task(client.thread_read("thread-b"))
    await wait_until(lambda: len(method_messages(server, "thread/read")) == 2)
    requests = method_messages(server, "thread/read")
    assert (await client.status()).pending_requests == 2

    server.respond_result(requests[1]["id"], {"thread": {"id": "thread-b"}})
    server.respond_result(requests[0]["id"], {"thread": {"id": "thread-a"}})

    assert (await first)["thread"] == {"id": "thread-a"}
    assert (await second)["thread"] == {"id": "thread-b"}
    assert (await client.status()).pending_requests == 0
    await client.close()


@pytest.mark.asyncio
async def test_structured_error_timeout_and_method_allowlist() -> None:
    server = FakeCodexServer()
    server.handlers["thread/read"] = lambda _message: (_ for _ in ()).throw(
        ServerRequestFailure(-32001, "missing thread", {"kind": "not_found"})
    )
    server.handlers["thread/list"] = lambda _message: NO_RESPONSE
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()

    with pytest.raises(CodexRequestError) as exc_info:
        await client.thread_read("missing")
    assert exc_info.value.code == -32001
    assert exc_info.value.data == {"kind": "not_found"}

    with pytest.raises(CodexRequestTimeout):
        await client.request("thread/list", {}, request_timeout=0.01)
    assert (await client.status()).pending_requests == 0

    with pytest.raises(UnsupportedCodexMethod):
        await client.request("command/exec", {"command": ["whoami"]})
    with pytest.raises(UnsupportedCodexMethod):
        await client.request("thread/shellCommand", {"command": "whoami"})
    assert "command/exec" not in SAFE_CLIENT_METHODS
    assert "thread/shellCommand" not in SAFE_CLIENT_METHODS
    await client.close()


@pytest.mark.asyncio
async def test_notifications_fan_out_without_cross_consumption() -> None:
    server = FakeCodexServer()
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()
    all_events = client.subscribe()
    turns = client.subscribe("turn/completed")

    server.notify("turn/started", {"threadId": "t", "turn": {"id": "one"}})
    server.notify("turn/completed", {"threadId": "t", "turn": {"id": "one"}})

    first_all = await asyncio.wait_for(all_events.get(), timeout=1)
    second_all = await asyncio.wait_for(all_events.get(), timeout=1)
    completed = await asyncio.wait_for(turns.get(), timeout=1)
    first_global = await asyncio.wait_for(client.notification_queue.get(), timeout=1)
    second_global = await asyncio.wait_for(client.notification_queue.get(), timeout=1)

    assert [first_all.method, second_all.method] == [
        "turn/started",
        "turn/completed",
    ]
    assert completed.method == "turn/completed"
    assert [first_global.method, second_global.method] == [
        "turn/started",
        "turn/completed",
    ]

    await all_events.close()
    with pytest.raises(StopAsyncIteration):
        await all_events.get()
    await client.close()
    assert turns.closed is True


@pytest.mark.asyncio
async def test_server_initiated_request_handlers_reply_without_blocking_reader() -> None:
    server = FakeCodexServer()

    class ApprovalHandler:
        async def handle(self, request: CodexServerRequest) -> Any:
            assert request.method == "item/commandExecution/requestApproval"
            await asyncio.sleep(0)
            return {"decision": "decline"}

    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    client.register_server_request_handler(
        "item/commandExecution/requestApproval", ApprovalHandler()
    )
    await client.start()

    server.request(
        "approval-1",
        "item/commandExecution/requestApproval",
        {"threadId": "thread-a", "turnId": "turn-a"},
    )
    server.notify("turn/started", {"threadId": "thread-b"})
    notification = await asyncio.wait_for(client.notification_queue.get(), timeout=1)
    assert notification.method == "turn/started"
    await wait_until(lambda: any(message.get("id") == "approval-1" for message in server.messages))
    response = next(message for message in server.messages if message.get("id") == "approval-1")
    assert response == {"id": "approval-1", "result": {"decision": "decline"}}

    server.request("unknown-1", "unknown/serverRequest", {})
    await wait_until(lambda: any(message.get("id") == "unknown-1" for message in server.messages))
    unknown = next(message for message in server.messages if message.get("id") == "unknown-1")
    assert unknown["error"]["code"] == -32601
    await client.close()


@pytest.mark.asyncio
async def test_process_exit_fails_pending_request_and_restart_recovers() -> None:
    first_server = FakeCodexServer()
    first_server.handlers["thread/read"] = lambda _message: NO_RESPONSE
    second_server = FakeCodexServer()
    second_server.handlers["thread/list"] = lambda _message: {"data": []}
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(first_server, second_server),
        codex_executable="codex.exe",
    )
    subscription = client.subscribe("turn/completed")
    await client.start()

    pending = asyncio.create_task(client.thread_read("thread-a"))
    await wait_until(lambda: len(method_messages(first_server, "thread/read")) == 1)
    first_server.process.exit(7)
    with pytest.raises(CodexAppServerConnectionError):
        await pending
    await wait_until(lambda: not client.is_running)
    failed = await client.status()
    assert failed.state == "failed"
    assert failed.initialized is False
    assert failed.last_error is not None

    await client.restart()
    recovered = await client.status()
    assert recovered.running is True
    assert recovered.restart_count == 1
    assert (await client.thread_list())["data"] == []
    second_server.notify("turn/completed", {"threadId": "thread-a"})
    assert (await asyncio.wait_for(subscription.get(), timeout=1)).method == "turn/completed"
    await client.close()


@pytest.mark.asyncio
async def test_close_escalates_to_direct_process_terminate() -> None:
    server = FakeCodexServer(exit_on_stdin_close=False)
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
        shutdown_timeout=0.01,
    )
    await client.start()

    await client.close()

    assert server.process.terminate_calls == 1
    assert server.process.kill_calls == 0


@pytest.mark.asyncio
async def test_thread_and_turn_wrappers_use_0144_6_field_names() -> None:
    server = FakeCodexServer()
    server.handlers.update(
        {
            "thread/list": lambda _message: {"data": []},
            "thread/read": lambda message: {"thread": {"id": message["params"]["threadId"]}},
            "thread/start": lambda _message: {"thread": {"id": "new-thread"}},
            "thread/resume": lambda message: {"thread": {"id": message["params"]["threadId"]}},
            "thread/archive": lambda _message: {},
            "turn/start": lambda _message: {"turn": {"id": "new-turn"}},
            "turn/steer": lambda message: {"turnId": message["params"]["expectedTurnId"]},
            "turn/interrupt": lambda _message: {},
        }
    )
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()

    await client.thread_list(
        limit=12,
        cwd=["C:\\one", "C:\\two"],
        search_term="training",
        sort_key="updated_at",
        sort_direction="desc",
    )
    await client.thread_read("thread-a")
    await client.thread_start(cwd="C:\\repo", model="fast-model")
    await client.thread_resume("thread-a", exclude_turns=True)
    await client.thread_archive("thread-a")
    await client.turn_start("thread-a", "do the work", effort="low")
    await client.turn_steer("thread-a", "turn-a", "stop batch jobs")
    await client.turn_interrupt("thread-a", "turn-a")

    list_params = method_messages(server, "thread/list")[0]["params"]
    assert list_params["searchTerm"] == "training"
    assert list_params["sortKey"] == "updated_at"
    assert list_params["cwd"] == ["C:\\one", "C:\\two"]
    assert method_messages(server, "thread/read")[0]["params"] == {
        "threadId": "thread-a",
        "includeTurns": True,
    }
    assert method_messages(server, "thread/start")[0]["params"] == {
        "cwd": "C:\\repo",
        "model": "fast-model",
        "serviceName": "agent-hotline",
    }
    assert method_messages(server, "thread/resume")[0]["params"] == {
        "threadId": "thread-a",
        "excludeTurns": True,
    }
    assert method_messages(server, "turn/start")[0]["params"] == {
        "threadId": "thread-a",
        "input": [{"type": "text", "text": "do the work"}],
        "effort": "low",
    }
    assert method_messages(server, "turn/steer")[0]["params"] == {
        "threadId": "thread-a",
        "expectedTurnId": "turn-a",
        "input": [{"type": "text", "text": "stop batch jobs"}],
    }
    assert method_messages(server, "turn/interrupt")[0]["params"] == {
        "threadId": "thread-a",
        "turnId": "turn-a",
    }
    await client.close()


@pytest.mark.asyncio
async def test_safe_controller_disambiguates_and_steers_exact_active_turn(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    active_thread = {
        "id": "thread-training",
        "name": "training-run",
        "preview": "Train the model",
        "cwd": str(allowed),
        "status": {"type": "active", "activeFlags": []},
        "updatedAt": 20,
        "turns": [{"id": "turn-active", "status": "inProgress", "items": []}],
    }
    another_thread = {
        "id": "thread-training-notes",
        "name": "training-notes",
        "preview": "Document training",
        "cwd": str(allowed),
        "status": {"type": "idle"},
        "updatedAt": 10,
        "turns": [],
    }
    outside_thread = {
        "id": "thread-secret",
        "name": "secret",
        "preview": "Outside",
        "cwd": str(outside),
        "status": {"type": "idle"},
        "updatedAt": 30,
        "turns": [],
    }
    threads = [active_thread, another_thread, outside_thread]
    server = FakeCodexServer()
    server.handlers["thread/list"] = lambda _message: {"data": threads}
    server.handlers["thread/read"] = lambda message: {
        "thread": next(
            thread for thread in threads if thread["id"] == message["params"]["threadId"]
        )
    }
    server.handlers["turn/steer"] = lambda message: {"turnId": message["params"]["expectedTurnId"]}
    server.handlers["turn/interrupt"] = lambda _message: {}
    server.handlers["thread/archive"] = lambda _message: {}
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()
    controller = SafeThreadController(client, workspace_roots=[allowed])

    resolved = await controller.resolve_thread("training-run")
    assert resolved.thread_id == "thread-training"
    with pytest.raises(AmbiguousThreadError):
        await controller.resolve_thread("training")
    with pytest.raises(ThreadNotFoundError):
        await controller.resolve_thread("secret")

    steered = await controller.send_instruction(
        "training-run", "Terminate the registered batch work and report status."
    )
    assert steered.action == "steered"
    assert steered.turn_id == "turn-active"
    steer_params = method_messages(server, "turn/steer")[0]["params"]
    assert steer_params["expectedTurnId"] == "turn-active"

    interrupted = await controller.pause("training-run")
    assert interrupted.turn_id == "turn-active"
    with pytest.raises(ThreadStateError):
        await controller.archive("training-run", confirmed_thread_id="thread-training-notes")
    archived = await controller.archive("training-run", confirmed_thread_id="thread-training")
    assert archived.action == "archived"
    await client.close()


@pytest.mark.asyncio
async def test_safe_controller_starts_root_only_inside_allowed_workspace(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    server = FakeCodexServer()
    server.handlers["thread/start"] = lambda _message: {"thread": {"id": "root-thread"}}
    server.handlers["turn/start"] = lambda _message: {"turn": {"id": "root-turn"}}
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()
    controller = SafeThreadController(client, workspace_roots=[allowed])

    with pytest.raises(UnsafeWorkspaceError, match="outside configured roots"):
        await controller.spawn_root(task="Investigate", cwd=outside)

    result = await controller.spawn_root(
        task="Investigate the deployment regression",
        cwd=allowed,
        model="fast-model",
        effort="low",
    )
    assert result.action == "spawned"
    assert result.thread_id == "root-thread"
    assert result.turn_id == "root-turn"
    assert method_messages(server, "thread/start")[0]["params"]["cwd"] == str(allowed.resolve())
    assert method_messages(server, "turn/start")[0]["params"]["effort"] == "low"
    await client.close()


@pytest.mark.asyncio
async def test_safe_controller_resumes_unloaded_thread_before_new_turn(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    stored = {
        "id": "stored-thread",
        "name": "stored",
        "preview": "Stored task",
        "cwd": str(allowed),
        "status": {"type": "notLoaded"},
        "updatedAt": 10,
        "turns": [],
    }
    resumed = {**stored, "status": {"type": "idle"}}
    server = FakeCodexServer()
    server.handlers["thread/list"] = lambda _message: {"data": [stored]}
    server.handlers["thread/read"] = lambda _message: {"thread": stored}
    server.handlers["thread/resume"] = lambda _message: {"thread": resumed}
    server.handlers["turn/start"] = lambda _message: {"turn": {"id": "next-turn"}}
    client = CodexAppServerClient(
        _process_factory=FakeProcessFactory(server),
        codex_executable="codex.exe",
    )
    await client.start()
    controller = SafeThreadController(client, workspace_roots=[allowed])

    result = await controller.send_instruction("stored", "Continue with the safe plan.")

    assert result.action == "started"
    assert result.turn_id == "next-turn"
    resume_message = method_messages(server, "thread/resume")[0]
    turn_message = method_messages(server, "turn/start")[0]
    assert server.messages.index(resume_message) < server.messages.index(turn_message)
    await client.close()
