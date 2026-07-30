from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from agent_hotline.api import create_app
from agent_hotline.contracts import (
    BeginInboundSessionRequest,
    ConfirmActionRequest,
    ExecuteActionRequest,
    PrepareActionRequest,
    RecordInstructionRequest,
    VoiceRepositoryContextRequest,
)
from agent_hotline.coordinator import HotlineCoordinator
from agent_hotline.providers import FakeCallProvider
from agent_hotline.repository_context import RepositoryContextService
from agent_hotline.settings import Settings
from agent_hotline.storage import NotFoundError, SQLiteStore

LOCAL_TOKEN = "repo-local-test-token-1234567890-abcdef"
TOOL_TOKEN = "repo-tool-test-token-1234567"
CALLBACK_TOKEN = "repo-action-signing-token-1234567890-abcdef"
OWNER_PIN = "246810"


@dataclass(slots=True)
class RepositoryHarness:
    client: httpx.AsyncClient
    repo: Path
    coordinator: HotlineCoordinator
    store: SQLiteStore


@pytest_asyncio.fixture
async def repository_api(tmp_path: Path) -> AsyncIterator[RepositoryHarness]:
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
        hotline_action_signing_secret=CALLBACK_TOKEN,
        owner_phone_number="+919876543210",
        owner_confirmation_pin=OWNER_PIN,
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
            yield RepositoryHarness(
                client=client,
                repo=repo,
                coordinator=app.state.coordinator,
                store=app.state.store,
            )


def _local_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_TOKEN}"}


def _tool_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOOL_TOKEN}"}


async def _create_outbound_event(
    harness: RepositoryHarness,
    summary: str,
    *,
    workspace: bool = True,
) -> str:
    context = {"workspace_ref": str(harness.repo)} if workspace else {}
    response = await harness.client.post(
        "/v1/escalations/notify",
        headers=_local_headers(),
        json={
            "source": "codex_mcp",
            "kind": "incident",
            "severity": "medium",
            "summary": summary,
            "question": "Confirm the next safe step.",
            "context": context,
            "wait_for_decision": False,
            "timeout_seconds": 1,
        },
    )
    assert response.status_code == 200
    return response.json()["event_id"]


def _voice_request(
    event_id: str,
    *,
    operation: str = "status",
    workspace: str | None = None,
    path: str | None = None,
    pin: str = OWNER_PIN,
) -> VoiceRepositoryContextRequest:
    return VoiceRepositoryContextRequest(
        event_id=event_id,
        workspace=workspace,
        operation=operation,
        path=path,
        confirmation_pin=pin,
    )


@pytest.mark.asyncio
async def test_local_repository_route_requires_local_token_and_is_read_only(
    repository_api: RepositoryHarness,
) -> None:
    missing = await repository_api.client.post(
        "/v1/repository/context",
        json={"operation": "search", "query": "bounded"},
    )
    crossed = await repository_api.client.post(
        "/v1/repository/context",
        json={"operation": "search", "query": "bounded"},
        headers=_tool_headers(),
    )
    accepted = await repository_api.client.post(
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
    repository_api: RepositoryHarness,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Repository evidence is needed.",
    )

    accepted = await repository_api.coordinator.query_repository_for_voice(
        _voice_request(event_id, operation="read", path="README.md")
    )
    with pytest.raises(PermissionError):
        await repository_api.coordinator.query_repository_for_voice(
            _voice_request(
                event_id,
                workspace="another-workspace",
            )
        )

    assert accepted.workspace == repository_api.repo.name
    assert accepted.items[0].path == "README.md"


@pytest.mark.asyncio
async def test_outbound_voice_repository_query_fails_closed_without_event_workspace(
    repository_api: RepositoryHarness,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Repository evidence is needed.",
        workspace=False,
    )

    with pytest.raises(PermissionError, match="event-bound workspace"):
        await repository_api.coordinator.query_repository_for_voice(_voice_request(event_id))


@pytest.mark.asyncio
async def test_inbound_voice_repository_query_requires_allowlisted_live_event(
    repository_api: RepositoryHarness,
) -> None:
    rejected = await repository_api.coordinator.begin_inbound_session(
        BeginInboundSessionRequest(
            caller_phone_number="+12025550199",
            interaction_id="repo-rejected",
        )
    )
    accepted = await repository_api.coordinator.begin_inbound_session(
        BeginInboundSessionRequest(
            caller_phone_number="+12025550147",
            interaction_id="repo-accepted",
        )
    )
    assert rejected.accepted is False
    assert accepted.accepted is True
    assert accepted.event_id is not None

    with pytest.raises(PermissionError, match="second-factor"):
        await repository_api.coordinator.query_repository_for_voice(
            _voice_request(
                accepted.event_id,
                workspace=repository_api.repo.name,
                pin="111111",
            )
        )
    result = await repository_api.coordinator.query_repository_for_voice(
        _voice_request(
            accepted.event_id,
            workspace=repository_api.repo.name,
            operation="tests",
        )
    )
    with pytest.raises(NotFoundError):
        await repository_api.coordinator.query_repository_for_voice(_voice_request("evt_unknown"))

    assert "does not execute tests" in result.summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["approve", "deny", "instruct", "defer", "auth_completed"],
)
async def test_repository_evidence_blocks_same_event_decisions_and_actions(
    repository_api: RepositoryHarness,
    outcome: str,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Inspect before deciding.",
    )
    exposed = await repository_api.coordinator.query_repository_for_voice(
        _voice_request(event_id, operation="read", path="README.md")
    )

    with pytest.raises(PermissionError, match="repository evidence"):
        await repository_api.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=event_id,
                outcome=outcome,
                instruction="Continue the agent task.",
                confirmation_pin=OWNER_PIN,
            )
        )
    with pytest.raises(PermissionError, match="repository evidence"):
        await repository_api.coordinator.prepare_action(
            PrepareActionRequest(
                event_id=event_id,
                action_type="demo.pause_deployment",
                parameters={},
            )
        )

    assert exposed.untrusted_data is True


@pytest.mark.asyncio
async def test_repository_evidence_blocks_same_event_action_confirmation(
    repository_api: RepositoryHarness,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Prepare before inspecting.",
    )
    prepared = await repository_api.coordinator.prepare_action(
        PrepareActionRequest(
            event_id=event_id,
            action_type="demo.pause_deployment",
            parameters={},
        )
    )
    phrase = prepared.exact_readback.rsplit("say exactly: ", 1)[1]
    await repository_api.coordinator.query_repository_for_voice(_voice_request(event_id))

    for pin in (OWNER_PIN, "111111"):
        with pytest.raises(PermissionError, match="repository evidence"):
            await repository_api.coordinator.confirm_action(
                ConfirmActionRequest(
                    event_id=event_id,
                    action_id=prepared.action_id,
                    confirmation_nonce=prepared.confirmation_nonce,
                    exact_confirmation=phrase,
                    confirmation_method="spoken_plus_dtmf",
                    confirmation_pin=pin,
                )
            )


@pytest.mark.asyncio
async def test_repository_evidence_blocks_consuming_an_existing_grant(
    repository_api: RepositoryHarness,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Confirm before inspecting.",
    )
    prepared = await repository_api.coordinator.prepare_action(
        PrepareActionRequest(
            event_id=event_id,
            action_type="demo.pause_deployment",
            parameters={},
        )
    )
    confirmed = await repository_api.coordinator.confirm_action(
        ConfirmActionRequest(
            event_id=event_id,
            action_id=prepared.action_id,
            confirmation_nonce=prepared.confirmation_nonce,
            exact_confirmation=prepared.exact_readback.rsplit("say exactly: ", 1)[1],
            confirmation_method="spoken_plus_dtmf",
            confirmation_pin=OWNER_PIN,
        )
    )
    assert confirmed.grant_id is not None
    await repository_api.coordinator.query_repository_for_voice(_voice_request(event_id))

    with pytest.raises(PermissionError, match="repository evidence"):
        await repository_api.coordinator.execute_action(
            ExecuteActionRequest(
                event_id=event_id,
                action_id=prepared.action_id,
                grant_id=confirmed.grant_id,
            )
        )


@pytest.mark.asyncio
async def test_failed_repository_read_still_makes_the_event_evidence_only(
    repository_api: RepositoryHarness,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Attempt a missing path.",
    )

    with pytest.raises(ValueError):
        await repository_api.coordinator.query_repository_for_voice(
            _voice_request(event_id, operation="read", path="missing-file.txt")
        )
    with pytest.raises(PermissionError, match="repository evidence"):
        await repository_api.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=event_id,
                outcome="instruct",
                instruction="Continue.",
                confirmation_pin=OWNER_PIN,
            )
        )


@pytest.mark.asyncio
async def test_repository_marker_wins_before_evidence_is_returned(
    repository_api: RepositoryHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_id = await _create_outbound_event(
        repository_api,
        "Race the evidence boundary.",
    )
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
            request,  # type: ignore[arg-type]
            forced_workspace=forced_workspace,
        )

    monkeypatch.setattr(RepositoryContextService, "query", delayed_query)
    repository_request = asyncio.create_task(
        repository_api.coordinator.query_repository_for_voice(_voice_request(event_id))
    )
    assert await asyncio.to_thread(started.wait, 2)
    with pytest.raises(PermissionError, match="repository evidence"):
        await repository_api.coordinator.record_instruction(
            RecordInstructionRequest(
                event_id=event_id,
                outcome="instruct",
                instruction="Continue.",
                confirmation_pin=OWNER_PIN,
            )
        )
    release.set()
    repository_response = await repository_request

    assert repository_response.untrusted_data is True
