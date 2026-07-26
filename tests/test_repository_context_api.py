from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from agent_hotline.api import create_app
from agent_hotline.providers import FakeCallProvider
from agent_hotline.repository_context import RepositoryContextService
from agent_hotline.settings import Settings

LOCAL_TOKEN = "repo-local-test-token-123456"
TOOL_TOKEN = "repo-tool-test-token-1234567"
CALLBACK_TOKEN = "repo-callback-test-token-123"


@pytest_asyncio.fixture
async def repository_api(
    tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, Path]]:
    repo = tmp_path / "repo"
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
    (repo / "README.md").write_text("bounded repository evidence\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_tool_token=TOOL_TOKEN,
        hotline_public_tools_require_token=True,
        hotline_callback_token=CALLBACK_TOKEN,
        owner_phone_number="+919876543210",
        owner_confirmation_pin="246810",
        hotline_allowlisted_callers="+12025550147",
        hotline_workspace_roots=str(repo),
        codex_app_server_enabled=False,
    )
    app = create_app(settings=settings, provider=FakeCallProvider())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client, repo


def _local_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_TOKEN}"}


def _tool_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOOL_TOKEN}"}


@pytest.mark.asyncio
async def test_local_repository_route_requires_local_token_and_is_read_only(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, _repo = repository_api
    missing = await client.post(
        "/v1/repository/context",
        json={"operation": "search", "query": "bounded"},
    )
    crossed = await client.post(
        "/v1/repository/context",
        json={"operation": "search", "query": "bounded"},
        headers=_tool_headers(),
    )
    accepted = await client.post(
        "/v1/repository/context",
        json={"operation": "search", "query": "bounded"},
        headers=_local_headers(),
    )

    assert missing.status_code == 401
    assert crossed.status_code == 403
    assert accepted.status_code == 200
    assert accepted.json()["items"][0]["path"] == "README.md"
    assert accepted.json()["untrusted_data"] is True


@pytest.mark.asyncio
async def test_outbound_voice_repository_query_is_live_and_event_workspace_bound(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, repo = repository_api
    escalation = await client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": "Repository evidence is needed.",
            "question": "No response is required.",
            "context": {"workspace_ref": str(repo)},
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    assert escalation.status_code == 200
    event_id = escalation.json()["event_id"]

    accepted = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "operation": "read",
            "path": "README.md",
            "confirmation_pin": "246810",
        },
    )
    escape = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "workspace": "another-workspace",
            "operation": "status",
            "confirmation_pin": "246810",
        },
    )

    assert accepted.status_code == 200
    assert accepted.json()["workspace"] == repo.name
    assert escape.status_code == 403


@pytest.mark.asyncio
async def test_outbound_voice_repository_query_fails_closed_without_event_workspace(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, _repo = repository_api
    escalation = await client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": "Repository evidence is needed.",
            "question": "No response is required.",
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    result = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": escalation.json()["event_id"],
            "operation": "status",
            "confirmation_pin": "246810",
        },
    )

    assert result.status_code == 403


@pytest.mark.asyncio
async def test_inbound_voice_repository_query_requires_allowlisted_live_event(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, repo = repository_api
    rejected_session = await client.post(
        "/v1/sarvam/tools/begin-inbound",
        headers=_tool_headers(),
        json={
            "caller_phone_number": "+12025550199",
            "interaction_id": "repo-rejected",
        },
    )
    accepted_session = await client.post(
        "/v1/sarvam/tools/begin-inbound",
        headers=_tool_headers(),
        json={
            "caller_phone_number": "+12025550147",
            "interaction_id": "repo-accepted",
        },
    )
    assert rejected_session.json()["accepted"] is False
    assert accepted_session.json()["accepted"] is True

    missing_pin = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": accepted_session.json()["event_id"],
            "workspace": repo.name,
            "operation": "status",
        },
    )
    wrong_pin = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": accepted_session.json()["event_id"],
            "workspace": repo.name,
            "operation": "status",
            "confirmation_pin": "111111",
        },
    )
    result = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": accepted_session.json()["event_id"],
            "workspace": repo.name,
            "operation": "tests",
            "confirmation_pin": "246810",
        },
    )
    unknown = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": "evt_unknown",
            "operation": "status",
            "confirmation_pin": "246810",
        },
    )
    unknown_wrong_pin = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": "evt_unknown",
            "operation": "status",
            "confirmation_pin": "111111",
        },
    )

    assert missing_pin.status_code == 422
    assert wrong_pin.status_code == 403
    assert result.status_code == 200
    assert "does not execute tests" in result.json()["summary"]
    assert unknown.status_code == 404
    assert unknown_wrong_pin.status_code == 404


@pytest.mark.asyncio
async def test_voice_repository_pin_attempts_have_strict_route_rate_limit(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, repo = repository_api
    session = await client.post(
        "/v1/sarvam/tools/begin-inbound",
        headers=_tool_headers(),
        json={
            "caller_phone_number": "+12025550147",
            "interaction_id": "repo-rate-limit",
        },
    )
    payload = {
        "event_id": session.json()["event_id"],
        "workspace": repo.name,
        "operation": "status",
        "confirmation_pin": "111111",
    }

    responses = [
        await client.post(
            "/v1/sarvam/tools/repository-context",
            headers=_tool_headers(),
            json=payload,
        )
        for _ in range(7)
    ]

    assert [response.status_code for response in responses[:6]] == [403] * 6
    assert responses[6].status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["approve", "deny", "instruct", "defer", "auth_completed"],
)
async def test_repository_evidence_blocks_same_event_decisions_and_actions(
    repository_api: tuple[httpx.AsyncClient, Path],
    outcome: str,
) -> None:
    client, repo = repository_api

    async def create_event(summary: str) -> str:
        response = await client.post(
            "/v1/escalations/notify",
            headers=_local_headers(),
            json={
                "source": "codex_mcp",
                "kind": "incident",
                "severity": "medium",
                "summary": summary,
                "question": "Confirm the next safe step.",
                "context": {"workspace_ref": str(repo)},
                "wait_for_decision": False,
                "timeout_seconds": 1,
            },
        )
        assert response.status_code == 200
        return response.json()["event_id"]

    decision_event = await create_event("Inspect before deciding.")
    exposed = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": decision_event,
            "operation": "read",
            "path": "README.md",
            "confirmation_pin": "246810",
        },
    )
    decision = await client.post(
        "/v1/sarvam/tools/record-instruction",
        headers=_tool_headers(),
        json={
            "event_id": decision_event,
            "outcome": outcome,
            "instruction": "Continue the agent task.",
            "confirmation_pin": "246810",
        },
    )
    prepare_after_exposure = await client.post(
        "/v1/sarvam/tools/prepare-action",
        headers=_tool_headers(),
        json={
            "event_id": decision_event,
            "action_type": "demo.pause_deployment",
            "parameters": {},
        },
    )

    assert exposed.status_code == 200
    assert decision.status_code == 403
    assert prepare_after_exposure.status_code == 403


@pytest.mark.asyncio
async def test_repository_evidence_blocks_same_event_action_confirmation(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, repo = repository_api
    created = await client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": "Prepare before inspecting.",
            "question": "Confirm the next safe step.",
            "context": {"workspace_ref": str(repo)},
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    assert created.status_code == 200
    confirm_event = created.json()["event_id"]
    prepared = await client.post(
        "/v1/sarvam/tools/prepare-action",
        headers=_tool_headers(),
        json={
            "event_id": confirm_event,
            "action_type": "demo.pause_deployment",
            "parameters": {},
        },
    )
    assert prepared.status_code == 200
    prepared_payload = prepared.json()
    exposed_before_confirm = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": confirm_event,
            "operation": "status",
            "confirmation_pin": "246810",
        },
    )
    blocked_confirmation = await client.post(
        "/v1/sarvam/tools/confirm-action",
        headers=_tool_headers(),
        json={
            "event_id": confirm_event,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": prepared_payload["exact_readback"].rsplit(
                "say exactly: ",
                1,
            )[1],
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": "246810",
        },
    )
    blocked_confirmation_wrong_pin = await client.post(
        "/v1/sarvam/tools/confirm-action",
        headers=_tool_headers(),
        json={
            "event_id": confirm_event,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": prepared_payload["exact_readback"].rsplit(
                "say exactly: ",
                1,
            )[1],
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": "111111",
        },
    )

    assert exposed_before_confirm.status_code == 200
    assert blocked_confirmation.status_code == 403
    assert blocked_confirmation_wrong_pin.status_code == 403


@pytest.mark.asyncio
async def test_repository_evidence_blocks_consuming_an_existing_grant(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, repo = repository_api
    created = await client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": "Confirm before inspecting.",
            "question": "Confirm the next safe step.",
            "context": {"workspace_ref": str(repo)},
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    assert created.status_code == 200
    event_id = created.json()["event_id"]
    prepared = await client.post(
        "/v1/sarvam/tools/prepare-action",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "action_type": "demo.pause_deployment",
            "parameters": {},
        },
    )
    assert prepared.status_code == 200
    prepared_payload = prepared.json()
    confirmed = await client.post(
        "/v1/sarvam/tools/confirm-action",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "action_id": prepared_payload["action_id"],
            "confirmation_nonce": prepared_payload["confirmation_nonce"],
            "exact_confirmation": prepared_payload["exact_readback"].rsplit(
                "say exactly: ",
                1,
            )[1],
            "confirmation_method": "spoken_plus_dtmf",
            "confirmation_pin": "246810",
        },
    )
    assert confirmed.status_code == 200
    exposed = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "operation": "status",
            "confirmation_pin": "246810",
        },
    )
    execution = await client.post(
        "/v1/sarvam/tools/execute-action",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "action_id": prepared_payload["action_id"],
            "grant_id": confirmed.json()["grant_id"],
        },
    )

    assert exposed.status_code == 200
    assert execution.status_code == 403


@pytest.mark.asyncio
async def test_failed_repository_read_still_makes_the_event_evidence_only(
    repository_api: tuple[httpx.AsyncClient, Path],
) -> None:
    client, repo = repository_api
    created = await client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": "Attempt a missing path.",
            "question": "Confirm the next safe step.",
            "context": {"workspace_ref": str(repo)},
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    event_id = created.json()["event_id"]
    missing_read = await client.post(
        "/v1/sarvam/tools/repository-context",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "operation": "read",
            "path": "missing-file.txt",
            "confirmation_pin": "246810",
        },
    )
    blocked = await client.post(
        "/v1/sarvam/tools/record-instruction",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "outcome": "instruct",
            "instruction": "Continue.",
            "confirmation_pin": "246810",
        },
    )

    assert missing_read.status_code == 400
    assert blocked.status_code == 403


@pytest.mark.asyncio
async def test_repository_marker_wins_before_evidence_is_returned(
    repository_api: tuple[httpx.AsyncClient, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repo = repository_api
    created = await client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": "Race the evidence boundary.",
            "question": "Confirm the next safe step.",
            "context": {"workspace_ref": str(repo)},
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    event_id = created.json()["event_id"]
    started = threading.Event()
    release = threading.Event()
    original_query = RepositoryContextService.query

    def delayed_query(
        service: RepositoryContextService,
        request: object,
        *,
        forced_workspace: str | None = None,
    ) -> object:
        started.set()
        release.wait(timeout=5)
        return original_query(
            service,
            request,
            forced_workspace=forced_workspace,
        )

    monkeypatch.setattr(RepositoryContextService, "query", delayed_query)
    repository_request = asyncio.create_task(
        client.post(
            "/v1/sarvam/tools/repository-context",
            headers=_tool_headers(),
            json={
                "event_id": event_id,
                "operation": "status",
                "confirmation_pin": "246810",
            },
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    blocked = await client.post(
        "/v1/sarvam/tools/record-instruction",
        headers=_tool_headers(),
        json={
            "event_id": event_id,
            "outcome": "instruct",
            "instruction": "Continue.",
            "confirmation_pin": "246810",
        },
    )
    release.set()
    repository_response = await repository_request

    assert blocked.status_code == 403
    assert repository_response.status_code == 200
