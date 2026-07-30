"""Black-box MCP compatibility checks over the real stdio transport."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from agent_hotline.api import create_app
from agent_hotline.contracts import RecordInstructionRequest
from agent_hotline.providers import FakeCallProvider
from agent_hotline.settings import Settings

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stdio_server_initializes_lists_tools_and_returns_structured_failure() -> None:
    """Exercise the same protocol path used by both Codex and Claude Code."""

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_hotline.mcp_server"],
        cwd=REPOSITORY_ROOT,
        env={
            # Port 9 is intentionally unavailable. The contact tool must return a
            # safe structured failure rather than corrupting the MCP session.
            "HOTLINE_DAEMON_URL": "http://127.0.0.1:9",
            "HOTLINE_LOCAL_TOKEN": "test-local-token-1234567890-abcdef",
            "HOTLINE_MCP_CLIENT": "codex",
        },
    )

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
        async with (
            stdio_client(server, errlog=stderr) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "agent-hotline"

            listed = await session.list_tools()
            by_name = {tool.name: tool for tool in listed.tools}
            assert set(by_name) == {
                "contact_human",
                "get_hotline_result",
                "hotline_status",
                "list_hotline_events",
                "notify_human",
                "query_repository_context",
                "request_authentication",
            }
            assert by_name["contact_human"].inputSchema["required"] == [
                "kind",
                "summary",
                "question",
            ]
            assert "source" not in by_name["contact_human"].inputSchema["properties"]
            assert "source" not in by_name["notify_human"].inputSchema["properties"]
            authentication_schema = by_name["request_authentication"].inputSchema
            assert authentication_schema["required"] == ["service", "reason"]
            assert {
                "service",
                "reason",
                "device_code_hint",
                "timeout_seconds",
            } == set(authentication_schema["properties"])
            assert {
                "url",
                "auth_url",
                "authentication_url",
                "login_url",
            }.isdisjoint(authentication_schema["properties"])
            assert by_name["contact_human"].annotations is not None
            assert by_name["contact_human"].annotations.readOnlyHint is False
            assert by_name["hotline_status"].annotations is not None
            assert by_name["hotline_status"].annotations.readOnlyHint is True
            assert by_name["query_repository_context"].annotations is not None
            assert by_name["query_repository_context"].annotations.readOnlyHint is True
            assert "operation" in by_name["query_repository_context"].inputSchema["required"]

            result = await session.call_tool(
                "contact_human",
                {
                    "kind": "clarification",
                    "summary": "A deterministic MCP transport smoke test.",
                    "question": "Should the caller remain safely paused?",
                    "timeout_seconds": 1,
                },
            )

        assert result.isError is False
        assert result.structuredContent is not None
        assert result.structuredContent["status"] == "failed"
        assert result.structuredContent["event_id"] == "unavailable"
        assert result.structuredContent["channel"] == "none"
        assert "unavailable" in result.structuredContent["failure_reason"].lower()
        # FastMCP may emit routine transport diagnostics to stderr, but the stream
        # must never contain a JSON-RPC protocol error or a leaked test token.
        stderr.seek(0)
        stderr_text = stderr.read()
        assert "test-local-token" not in stderr_text
        assert '"error"' not in stderr_text


def test_plugin_mcp_manifest_uses_portable_command_only() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "plugins" / "agent-hotline" / ".mcp.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest == {
        "mcpServers": {
            "agent_hotline": {
                "command": "agent-hotline-mcp",
                "args": [],
                "env": {"HOTLINE_MCP_CLIENT": "codex"},
            }
        }
    }


@pytest.mark.asyncio
async def test_stdio_mcp_queries_real_loopback_daemon_routes(
    tmp_path: Path,
) -> None:
    """Prove Codex/Claude can query context and recover a call result via MCP."""

    repo = tmp_path / "mcp-repo"
    repo.mkdir()
    git_init = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        "init",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert await git_init.wait() == 0
    (repo / "README.md").write_text("mcp-repo-needle\n", encoding="utf-8")
    local_token = "mcp-repository-local-token-1234567890"
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_transport="fake",
        hotline_local_token=local_token,
        hotline_action_signing_secret="mcp-action-signing-token-1234567890",
        owner_phone_number="+919876543210",
        owner_confirmation_pin="246810",
        hotline_workspace_roots=str(repo),
        codex_app_server_enabled=False,
    )
    app = create_app(settings=settings, provider=FakeCallProvider())
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started

    mcp_parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "agent_hotline.mcp_server"],
        cwd=REPOSITORY_ROOT,
        env={
            "HOTLINE_DAEMON_URL": f"http://127.0.0.1:{port}",
            "HOTLINE_LOCAL_TOKEN": local_token,
            "HOTLINE_MCP_CLIENT": "codex",
        },
    )
    try:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr:
            async with (
                stdio_client(mcp_parameters, errlog=stderr) as (read_stream, write_stream),
                ClientSession(read_stream, write_stream) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "query_repository_context",
                    {
                        "operation": "search",
                        "query": "mcp-repo-needle",
                    },
                )
                started = await session.call_tool(
                    "contact_human",
                    {
                        "kind": "incident",
                        "summary": "A recoverable MCP polling test.",
                        "question": "Should this task remain paused?",
                        "dedupe_key": "mcp-result-polling-test",
                        "timeout_seconds": 60,
                    },
                )
                assert started.isError is False
                assert started.structuredContent is not None
                assert started.structuredContent["status"] == "calling"
                event_id = started.structuredContent["event_id"]

                pending = await session.call_tool(
                    "get_hotline_result",
                    {"event_id": event_id},
                )
                assert pending.isError is False
                assert pending.structuredContent is not None
                assert pending.structuredContent["status"] == "calling"
                assert pending.structuredContent["outcome"] == "none"
                assert pending.structuredContent["identity_verified"] is False

                recorded = await app.state.coordinator.record_instruction(
                    RecordInstructionRequest(
                        event_id=event_id,
                        outcome="instruct",
                        instruction="Keep the task paused while I inspect it.",
                        confirmation_pin="246810",
                    )
                )
                resolved = await session.call_tool(
                    "get_hotline_result",
                    {"event_id": event_id},
                )
            assert result.isError is False
            assert result.structuredContent is not None
            assert result.structuredContent["operation"] == "search"
            assert result.structuredContent["items"][0]["path"] == "README.md"
            assert resolved.isError is False
            assert resolved.structuredContent is not None
            assert resolved.structuredContent["status"] == "resolved"
            assert resolved.structuredContent["outcome"] == "instruct"
            assert resolved.structuredContent["identity_verified"] is True
            assert resolved.structuredContent["decision_id"] == recorded.decision_id
            assert (
                resolved.structuredContent["instruction"]
                == "Keep the task paused while I inspect it."
            )
            stderr.seek(0)
            assert local_token not in stderr.read()
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=10)
