from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

from agent_hotline.api import create_app
from agent_hotline.coordinator import HotlineCoordinator
from agent_hotline.fallback_delivery import (
    FallbackDeliveryError,
    FallbackNotification,
    WebhookFallbackNotifier,
)
from agent_hotline.models import (
    ContactDirection,
    ContactSession,
    EscalationEvent,
    EscalationKind,
    EventState,
    FallbackLink,
    FallbackState,
    ProviderWebhookPayload,
    SessionState,
    Severity,
    utc_now,
)
from agent_hotline.providers import FakeCallProvider
from agent_hotline.settings import Settings
from agent_hotline.storage import SQLiteStore

LOCAL_TOKEN = "local-fallback-token-1234567890-abcdef"
CALLBACK_TOKEN = "action-fallback-token-1234567890-abcdef"
WEBHOOK_TOKEN = "webhook-fallback-token-1234567890-abcdef"
OWNER_PIN = "246810"


@dataclass(slots=True)
class RecordingNotifier:
    notifications: list[FallbackNotification] = field(default_factory=list)
    fail: bool = False
    closed: bool = False

    async def send(self, notification: FallbackNotification) -> None:
        if self.fail:
            raise FallbackDeliveryError("synthetic delivery failure")
        self.notifications.append(notification)

    async def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class FallbackHarness:
    client: httpx.AsyncClient
    provider: FakeCallProvider
    notifier: RecordingNotifier
    store: SQLiteStore
    database_path: Path
    coordinator: HotlineCoordinator


@pytest_asyncio.fixture
async def fallback_api(tmp_path: Path) -> AsyncIterator[FallbackHarness]:
    database_path = tmp_path / "fallback.sqlite3"
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=database_path,
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_action_signing_secret=CALLBACK_TOKEN,
        hotline_fallback_signing_secret="fallback-signing-token-1234567890-abcdef",
        public_base_url="https://hotline.example.invalid",
        hotline_fallback_webhook_url="https://push.example.invalid/hotline",
        hotline_fallback_webhook_token=WEBHOOK_TOKEN,
        hotline_fallback_ttl_seconds=300,
        owner_phone_number="+919876543210",
        owner_confirmation_pin=OWNER_PIN,
        codex_app_server_enabled=False,
    )
    provider = FakeCallProvider()
    notifier = RecordingNotifier()
    app = create_app(
        settings=settings,
        provider=provider,
        fallback_notifier=notifier,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield FallbackHarness(
                client=client,
                provider=provider,
                notifier=notifier,
                store=app.state.store,
                database_path=database_path,
                coordinator=app.state.coordinator,
            )


def _contact_payload() -> dict[str, object]:
    return {
        "source": "codex_mcp",
        "kind": "clarification",
        "severity": "high",
        "summary": "The release is waiting on an owner decision.",
        "question": "Approve the pending release step, deny it, or provide constraints?",
        "context": {
            "thread_id": "thread-fallback",
            "workspace_ref": "workspace-fallback",
            "pending_action_summary": "Continue the already-tested release step.",
            "owner_constraints": ["Do not change production credentials."],
        },
        "dedupe_key": "fallback-release-decision-v1",
        "no_answer_policy": "defer",
        "wait_for_decision": True,
        "timeout_seconds": 5,
    }


def _local_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_TOKEN}"}


async def _wait_for_provider_call(
    provider: FakeCallProvider,
    store: SQLiteStore,
) -> str:
    for _ in range(200):
        if provider.calls:
            event_id = provider.calls[-1][0]
            attempt_id = f"fake-attempt-{event_id}"
            if await store.get_session_by_attempt(attempt_id) is not None:
                return event_id
        await asyncio.sleep(0.01)
    pytest.fail("fake provider call was not durably linked")


async def test_missed_call_fallback_is_pin_gated_and_consumed_once(
    fallback_api: FallbackHarness,
) -> None:
    pending = asyncio.create_task(
        fallback_api.client.post(
            "/v1/escalations/contact",
            json=_contact_payload(),
            headers=_local_headers(),
        )
    )
    event_id = await _wait_for_provider_call(
        fallback_api.provider,
        fallback_api.store,
    )
    webhook = await fallback_api.coordinator.reconcile_provider_completion(
        ProviderWebhookPayload(
            attempt_id=f"fake-attempt-{event_id}",
            status="no_answer",
            provider="fake",
            failure_reason="The owner did not answer.",
        )
    )

    assert webhook == {
        "accepted": True,
        "created": True,
        "event_id": event_id,
        "status": "no_answer",
    }
    assert len(fallback_api.notifier.notifications) == 1
    assert not pending.done()
    event = await fallback_api.store.require_event(event_id)
    assert event.state is EventState.FALLBACK_PENDING

    notification = fallback_api.notifier.notifications[0]
    secure_url = notification.secure_url.get_secret_value()
    split = urlsplit(secure_url)
    link_token = split.fragment
    assert split.path == "/fallback"
    assert link_token
    assert link_token not in split.path

    page = await fallback_api.client.get("/fallback")
    javascript = await fallback_api.client.get("/fallback/assets/fallback.js")
    assert page.status_code == 200
    assert link_token not in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]
    assert "location.hash.slice(1)" in javascript.text
    assert "innerHTML" not in javascript.text

    wrong_pin = await fallback_api.client.post(
        "/v1/fallback/open",
        json={"token": link_token, "confirmation_pin": "135790"},
    )
    assert wrong_pin.status_code == 403
    assert "release" not in wrong_pin.text.lower()

    opened = await fallback_api.client.post(
        "/v1/fallback/open",
        json={"token": link_token, "confirmation_pin": OWNER_PIN},
    )
    assert opened.status_code == 200
    opened_payload = opened.json()
    assert opened_payload["summary"] == _contact_payload()["summary"]
    assert opened_payload["pending_action_summary"] == ("Continue the already-tested release step.")
    submission_token = opened_payload["submission_token"]

    decided = await fallback_api.client.post(
        "/v1/fallback/decision",
        json={
            "submission_token": submission_token,
            "outcome": "approve",
            "instruction": "Approve only this release step. Keep the stated constraint.",
            "confirmed": True,
        },
    )
    result = await asyncio.wait_for(pending, timeout=2)
    replay = await fallback_api.client.post(
        "/v1/fallback/decision",
        json={
            "submission_token": submission_token,
            "outcome": "approve",
            "instruction": "Approve only this release step. Keep the stated constraint.",
            "confirmed": True,
        },
    )

    assert decided.status_code == 200
    assert result.status_code == 200
    assert result.json()["status"] == "resolved"
    assert result.json()["outcome"] == "approve"
    assert result.json()["identity_verified"] is True
    assert replay.status_code == 409
    fallback = await fallback_api.store.get_fallback_for_event(event_id)
    assert fallback is not None
    assert fallback.state is FallbackState.CONSUMED
    decision = await fallback_api.store.get_decision(event_id)
    assert decision is not None
    assert decision.source.value == "secure_fallback"
    assert decision.channel.value == "web"
    assert decision.approved_action_ids == []

    durable_bytes = b"".join(
        path.read_bytes()
        for path in fallback_api.database_path.parent.glob(f"{fallback_api.database_path.name}*")
        if path.is_file()
    )
    for secret in (OWNER_PIN, link_token, submission_token, secure_url):
        assert secret.encode("utf-8") not in durable_bytes


async def test_fallback_delivery_failure_preserves_no_answer(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        hotline_env="test",
        hotline_database_path=tmp_path / "delivery-failure.sqlite3",
        hotline_transport="fake",
        hotline_local_token=LOCAL_TOKEN,
        hotline_action_signing_secret=CALLBACK_TOKEN,
        hotline_fallback_signing_secret="fallback-signing-token-1234567890-abcdef",
        public_base_url="https://hotline.example.invalid",
        owner_phone_number="+919876543210",
        owner_confirmation_pin=OWNER_PIN,
        codex_app_server_enabled=False,
    )
    provider = FakeCallProvider()
    notifier = RecordingNotifier(fail=True)
    app = create_app(
        settings=settings,
        provider=provider,
        fallback_notifier=notifier,
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            pending = asyncio.create_task(
                client.post(
                    "/v1/escalations/contact",
                    json=_contact_payload(),
                    headers=_local_headers(),
                )
            )
            event_id = await _wait_for_provider_call(
                provider,
                app.state.store,
            )
            webhook = await app.state.coordinator.reconcile_provider_completion(
                ProviderWebhookPayload(
                    attempt_id=f"fake-attempt-{event_id}",
                    status="busy",
                    provider="fake",
                )
            )
            result = await asyncio.wait_for(pending, timeout=2)
            fallback = await app.state.store.get_fallback_for_event(event_id)
            decision = await app.state.store.get_decision(event_id)

    assert webhook["accepted"] is True
    assert webhook["created"] is True
    assert result.json()["status"] == "busy"
    assert result.json()["identity_verified"] is False
    assert fallback is not None
    assert fallback.state is FallbackState.DELIVERY_FAILED
    assert decision is None


async def test_expired_fallback_fails_waiting_event(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "expiry.sqlite3")
    await store.initialize()
    event = EscalationEvent(
        kind=EscalationKind.AMBIGUITY,
        severity=Severity.WARNING,
        summary="The agent needs a choice.",
        question="Which safe option should it use?",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.PENDING,
        )
    )
    created_at = utc_now()
    fallback = await store.create_fallback(
        FallbackLink(
            event_id=event.event_id,
            session_id=session.session_id,
            reason="call_no_answer_without_decision",
            created_at=created_at,
            expires_at=created_at + timedelta(minutes=2),
        )
    )
    await store.activate_fallback(fallback.fallback_id, now=created_at)

    expired_event_ids = await store.expire_fallbacks(now=created_at + timedelta(minutes=3))
    expired = await store.get_fallback(fallback.fallback_id)
    durable_event = await store.require_event(event.event_id)
    await store.close()

    assert expired_event_ids == [event.event_id]
    assert expired is not None
    assert expired.state is FallbackState.EXPIRED
    assert durable_event.state is EventState.FAILED


async def test_webhook_notifier_sends_only_generic_context() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = WebhookFallbackNotifier(
        url="https://push.example.invalid/hotline",
        token=SecretStr(WEBHOOK_TOKEN),
        client=client,
    )
    notification = FallbackNotification(
        fallback_id="fbk_delivery_test",
        event_id="evt_delivery_test",
        secure_url=SecretStr("https://hotline.example.invalid/fallback#signed-one-time-token"),
        expires_at=utc_now() + timedelta(minutes=10),
    )
    await notifier.send(notification)
    await notifier.close()
    await client.aclose()

    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert request.headers["Authorization"] == f"Bearer {WEBHOOK_TOKEN}"
    assert request.headers["Idempotency-Key"] == notification.fallback_id
    assert payload["url"].endswith("#signed-one-time-token")
    serialized = request.content.decode("utf-8")
    assert "workspace" not in serialized
    assert "summary" not in serialized
    assert "question" not in serialized
