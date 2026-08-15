from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from agent_hotline.api import create_app
from agent_hotline.codex_protocol import (
    ThreadCandidate,
    ThreadControlResult,
    ThreadWritePlan,
)
from agent_hotline.providers import FakeCallProvider
from agent_hotline.settings import Settings

VAPI_TOKEN = "vapi-webhook-test-token-1234567890"
ASSISTANT_ID = "edcc6053-2119-4259-b433-0f73ecae3351"
PHONE_NUMBER_ID = "6e5c053b-afb8-4916-b943-5823121a46c8"
OWNER_NUMBER = "+919999890760"


class VapiThreadController:
    def __init__(self, workspace: Path) -> None:
        self.workspace = str(workspace.resolve())
        self.spawn_calls: list[tuple[str, str]] = []
        self.instruction_calls: list[tuple[str, str]] = []

    async def list_candidates(
        self,
        *,
        limit: int = 100,
        archived: bool = False,
    ) -> tuple[ThreadCandidate, ...]:
        del archived
        return (
            ThreadCandidate(
                thread_id="thread-running",
                name="Build demo",
                preview="Finish Better Call Sol tools.",
                cwd=self.workspace,
                status="active",
                updated_at=10,
            ),
        )[:limit]

    async def search_candidates(
        self,
        query: str,
        *,
        limit: int = 100,
    ) -> tuple[ThreadCandidate, ...]:
        candidates = await self.list_candidates(limit=limit)
        normalized = query.casefold()
        return tuple(
            item
            for item in candidates
            if normalized in f"{item.name} {item.preview} {item.status}".casefold()
        )

    async def inspect_thread(self, reference: str) -> dict[str, Any]:
        return {
            "thread": {
                "id": reference,
                "name": "Build demo",
                "preview": "Finish Better Call Sol tools.",
                "cwd": self.workspace,
                "status": {"type": "active", "activeFlags": []},
                "turns": [
                    {"id": "turn-running", "status": {"type": "inProgress"}}
                ],
            }
        }

    def canonicalize_workspace(self, reference: str) -> str:
        if reference not in {".", self.workspace}:
            raise PermissionError("workspace is not allowlisted")
        return self.workspace

    async def spawn_root(self, *, task: str, cwd: str) -> ThreadControlResult:
        self.spawn_calls.append((task, cwd))
        return ThreadControlResult(
            action="spawned",
            thread_id="thread-spawned",
            turn_id="turn-spawned",
            response={},
        )

    async def prepare_instruction(
        self,
        reference: str,
        instruction: str,
    ) -> ThreadWritePlan:
        return ThreadWritePlan(
            thread_id=reference,
            cwd=self.workspace,
            operation="steer",
            turn_id="turn-running",
            state_fingerprint="voice-test-fingerprint",
            instruction=instruction,
        )

    async def execute_instruction(self, plan: ThreadWritePlan) -> ThreadControlResult:
        self.instruction_calls.append((plan.thread_id, plan.instruction))
        return ThreadControlResult(
            action="steered",
            thread_id=plan.thread_id,
            turn_id=plan.turn_id,
            response={},
        )


@pytest_asyncio.fixture
async def vapi_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, VapiThreadController]]:
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_transport="fake",
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_local_token="local-vapi-test-token-1234567890-abcdef",
        hotline_action_signing_secret="action-vapi-test-token-1234567890-abcdef",
        vapi_webhook_token=VAPI_TOKEN,
        vapi_assistant_id=ASSISTANT_ID,
        vapi_phone_number_id=PHONE_NUMBER_ID,
        owner_phone_number=OWNER_NUMBER,
        owner_confirmation_pin="246810",
        codex_app_server_enabled=False,
        hotline_allow_codex_writes=True,
        hotline_demo_auto_execute_actions=True,
        hotline_workspace_roots=str(tmp_path),
    )
    controller = VapiThreadController(tmp_path)
    app = create_app(
        settings=settings,
        provider=FakeCallProvider(),
        controller=controller,  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, controller


def _tool_payload(
    *,
    call_id: str,
    tool_call_id: str,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "message": {
            "type": "tool-calls",
            "call": {
                "id": call_id,
                "assistantId": ASSISTANT_ID,
                "phoneNumberId": PHONE_NUMBER_ID,
                "customer": {"number": OWNER_NUMBER},
            },
            "toolCallList": [
                {
                    "id": tool_call_id,
                    "name": name,
                    "arguments": arguments,
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_vapi_webhook_requires_bearer(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    response = await client.post(
        "/v1/vapi/webhook",
        json=_tool_payload(
            call_id="call-auth",
            tool_call_id="tool-auth",
            name="list_codex_tasks",
            arguments={},
        ),
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_vapi_list_tool_returns_required_string_result(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    response = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        json=_tool_payload(
            call_id="call-list",
            tool_call_id="tool-list",
            name="list_codex_tasks",
            arguments={"query": "running", "limit": 5},
        ),
    )
    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["toolCallId"] == "tool-list"
    result = json.loads(item["result"])
    assert result["threads"][0]["thread_id"] == "thread-running"
    assert "\n" not in item["result"]


@pytest.mark.asyncio
async def test_vapi_inspect_normalizes_structured_statuses(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    response = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        json=_tool_payload(
            call_id="call-inspect",
            tool_call_id="tool-inspect",
            name="inspect_codex_task",
            arguments={"reference": "thread-running"},
        ),
    )

    result = json.loads(response.json()["results"][0]["result"])
    assert result["status"] == "active"
    assert result["recent_turns"] == [
        {"turn_id": "turn-running", "status": "inProgress"}
    ]


@pytest.mark.asyncio
async def test_vapi_demo_spawn_executes_once_and_replays_receipt(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, controller = vapi_client
    payload = _tool_payload(
        call_id="call-spawn",
        tool_call_id="tool-spawn",
        name="spawn_codex_task",
        arguments={"task": "Check the hackathon demo", "cwd": "."},
    )
    first = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        json=payload,
    )
    second = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        json=payload,
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    result = json.loads(first.json()["results"][0]["result"])
    assert result["accepted"] is True
    assert result["queued"] is True
    assert result["executed"] is False
    assert result["result"]["action"] == "spawn_queued"
    for _ in range(100):
        if controller.spawn_calls:
            break
        await asyncio.sleep(0.01)
    assert controller.spawn_calls == [
        ("Check the hackathon demo", controller.workspace)
    ]


@pytest.mark.asyncio
async def test_vapi_manage_tool_maps_spawn_action(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, controller = vapi_client
    response = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        json=_tool_payload(
            call_id="call-manage-spawn",
            tool_call_id="tool-manage-spawn",
            name="manage_codex_task",
            arguments={"action": "spawn", "task": "Make a launch checklist"},
        ),
    )

    assert response.status_code == 200
    result = json.loads(response.json()["results"][0]["result"])
    assert result["queued"] is True
    assert result["executed"] is False
    for _ in range(100):
        if controller.spawn_calls:
            break
        await asyncio.sleep(0.01)
    assert controller.spawn_calls == [("Make a launch checklist", controller.workspace)]


@pytest.mark.asyncio
async def test_vapi_manage_tool_queues_existing_thread_instruction(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, controller = vapi_client
    response = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        json=_tool_payload(
            call_id="call-manage-instruct",
            tool_call_id="tool-manage-instruct",
            name="manage_codex_task",
            arguments={
                "action": "instruct",
                "reference": "thread-running",
                "instruction": "Check production health.",
            },
        ),
    )

    assert response.status_code == 200
    result = json.loads(response.json()["results"][0]["result"])
    assert result["queued"] is True
    assert result["executed"] is False
    assert result["result"]["action"] == "instruction_queued"
    for _ in range(100):
        if controller.instruction_calls:
            break
        await asyncio.sleep(0.01)
    assert controller.instruction_calls == [
        ("thread-running", "Check production health.")
    ]


@pytest.mark.asyncio
async def test_vapi_end_report_releases_owner_session(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    headers = {"Authorization": f"Bearer {VAPI_TOKEN}"}
    started = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json=_tool_payload(
            call_id="call-one",
            tool_call_id="tool-one",
            name="list_codex_tasks",
            arguments={},
        ),
    )
    assert "result" in started.json()["results"][0]

    ended = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "call-one",
                    "assistantId": ASSISTANT_ID,
                    "phoneNumberId": PHONE_NUMBER_ID,
                    "customer": {"number": OWNER_NUMBER},
                },
                "endedReason": "customer-ended-call",
                "durationSeconds": 12,
            }
        },
    )
    assert ended.status_code == 200
    assert ended.json()["status"] == "completed"

    next_call = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json=_tool_payload(
            call_id="call-two",
            tool_call_id="tool-two",
            name="list_codex_tasks",
            arguments={},
        ),
    )
    assert "result" in next_call.json()["results"][0]


@pytest.mark.asyncio
async def test_vapi_end_report_accepts_bounded_transcript_artifact(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    headers = {"Authorization": f"Bearer {VAPI_TOKEN}"}
    started = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json=_tool_payload(
            call_id="call-large-terminal",
            tool_call_id="tool-large-terminal",
            name="list_codex_tasks",
            arguments={},
        ),
    )
    assert started.status_code == 200

    ended = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "call-large-terminal",
                    "assistantId": ASSISTANT_ID,
                    "phoneNumberId": PHONE_NUMBER_ID,
                    "customer": {"number": OWNER_NUMBER},
                },
                "endedReason": "customer-ended-call",
                "durationSeconds": 90,
                "artifact": {"transcript": "x" * 100_000},
            }
        },
    )

    assert ended.status_code == 200
    assert ended.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_vapi_webhook_still_rejects_unbounded_payload(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    response = await client.post(
        "/v1/vapi/webhook",
        headers={"Authorization": f"Bearer {VAPI_TOKEN}"},
        content=b"x" * (512 * 1024 + 1),
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


@pytest.mark.asyncio
async def test_vapi_minimal_documented_end_report_releases_session(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    headers = {"Authorization": f"Bearer {VAPI_TOKEN}"}
    started = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json=_tool_payload(
            call_id="call-minimal-terminal",
            tool_call_id="tool-minimal-terminal",
            name="list_codex_tasks",
            arguments={},
        ),
    )
    assert "result" in started.json()["results"][0]

    ended = await client.post(
        "/v1/vapi/webhook",
        headers=headers,
        json={
            "message": {
                "type": "end-of-call-report",
                "call": {
                    "id": "call-minimal-terminal",
                    "status": "ended",
                    "endedReason": "customer-ended-call",
                    "durationSeconds": 12,
                },
            }
        },
    )

    assert ended.status_code == 200
    assert ended.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_vapi_rejects_missing_or_mismatched_resource_binding_with_http_200(
    vapi_client: tuple[httpx.AsyncClient, VapiThreadController],
) -> None:
    client, _controller = vapi_client
    headers = {"Authorization": f"Bearer {VAPI_TOKEN}"}
    payload = _tool_payload(
        call_id="call-wrong-resource",
        tool_call_id="tool-wrong-resource",
        name="list_codex_tasks",
        arguments={},
    )
    call = payload["message"]["call"]  # type: ignore[index]
    assert isinstance(call, dict)
    call.pop("assistantId")

    missing = await client.post("/v1/vapi/webhook", headers=headers, json=payload)
    call["assistantId"] = "another-assistant"
    mismatched = await client.post("/v1/vapi/webhook", headers=headers, json=payload)

    assert missing.status_code == mismatched.status_code == 200
    assert "does not match" in missing.json()["results"][0]["error"]
    assert "does not match" in mismatched.json()["results"][0]["error"]
