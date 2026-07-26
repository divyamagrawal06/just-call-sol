from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_hotline.dashboard import create_dashboard_router
from agent_hotline.models import EscalationEvent, TimelineEntry, TimelineKind


class FakeDashboardStore:
    def __init__(
        self,
        events: list[EscalationEvent],
        timeline: list[TimelineEntry],
    ) -> None:
        self.events = events
        self.timeline = timeline
        self.calls = 0

    async def list_events(self, *, limit: int = 50, states=None):
        self.calls += 1
        return self.events[:limit]

    async def get_event(self, event_id: str):
        return next((event for event in self.events if event.event_id == event_id), None)

    async def list_timeline(
        self,
        *,
        event_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ):
        entries = [entry for entry in self.timeline if entry.event_id == event_id]
        return entries[:limit]


class FailingDashboardStore(FakeDashboardStore):
    async def list_events(self, *, limit: int = 50, states=None):
        raise RuntimeError("database password=do-not-leak +12025550147")


def build_client(store: FakeDashboardStore, *, remote: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(create_dashboard_router(store))
    if remote:
        return TestClient(
            app,
            base_url="https://dashboard.example",
            client=("127.0.0.1", 51000),
        )
    return TestClient(
        app,
        base_url="http://localhost",
        client=("127.0.0.1", 51000),
    )


def example_data() -> tuple[list[EscalationEvent], list[TimelineEntry]]:
    event = EscalationEvent(
        event_id="evt_dashboard-example",
        kind="incident",
        severity="critical",
        summary=("Database exhausted RUs. Call +1 202 555 0147; token=dashboard-secret-value"),
        question="This field must not be rendered.",
        workspace="C:/private/workspace",
        evidence={"query": "private evidence"},
        state="dialing",
        detected_at=datetime(2026, 7, 26, 1, 2, tzinfo=UTC),
    )
    timeline = TimelineEntry(
        event_id=event.event_id,
        kind=TimelineKind.EVENT_STATE_CHANGED,
        from_state="queued",
        to_state="dialing",
        details={
            "message": "Called +12025550147 with password=hunter2",
            "attempts": 2,
            "provider_payload": "must not render",
        },
        occurred_at=datetime(2026, 7, 26, 1, 3, tzinfo=UTC),
    )
    return [event], [timeline]


def test_snapshot_is_read_only_allowlisted_and_redacted() -> None:
    events, timeline = example_data()
    client = build_client(FakeDashboardStore(events, timeline))

    response = client.get("/dashboard/api/snapshot")

    assert response.status_code == 200
    payload = response.json()
    serialized = response.text
    assert payload["read_only"] is True
    assert payload["state_counts"]["dialing"] == 1
    assert payload["events"][0]["summary"].endswith("[REDACTED]")
    assert payload["events"][0]["timeline"][0]["detail"] == {
        "message": "Called [REDACTED PHONE] with password=[REDACTED]"
    }
    for private_value in (
        "+1 202 555 0147",
        "+12025550147",
        "dashboard-secret-value",
        "hunter2",
        "private evidence",
        "C:/private/workspace",
        "This field must not be rendered.",
        "must not render",
    ):
        assert private_value not in serialized
    assert "question" not in payload["events"][0]
    assert "evidence" not in payload["events"][0]
    assert "workspace" not in payload["events"][0]
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_dashboard_serves_external_template_and_assets_without_inline_script() -> None:
    events, timeline = example_data()
    client = build_client(FakeDashboardStore(events, timeline))

    response = client.get("/dashboard/")
    javascript = client.get("/dashboard/assets/dashboard.js")
    stylesheet = client.get("/dashboard/assets/dashboard.css")

    assert response.status_code == 200
    assert '<script src="/dashboard/assets/dashboard.js" defer></script>' in response.text
    assert "<script>" not in response.text
    assert javascript.status_code == 200
    assert javascript.headers["content-type"].startswith("application/javascript")
    assert "textContent" in javascript.text
    assert "innerHTML" not in javascript.text
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")


def test_router_has_no_mutating_endpoints() -> None:
    events, timeline = example_data()
    client = build_client(FakeDashboardStore(events, timeline))

    assert client.post("/dashboard/api/snapshot").status_code == 405
    assert client.put("/dashboard/api/snapshot").status_code == 405
    assert client.delete("/dashboard/api/snapshot").status_code == 405


def test_remote_host_is_rejected_even_when_reverse_proxy_peer_is_local() -> None:
    events, timeline = example_data()
    store = FakeDashboardStore(events, timeline)
    client = build_client(store, remote=True)

    response = client.get("/dashboard/api/snapshot")

    assert response.status_code == 403
    assert store.calls == 0
    assert "+12025550147" not in response.text


def test_store_failures_are_generic_and_never_echo_exception_details() -> None:
    events, timeline = example_data()
    client = build_client(FailingDashboardStore(events, timeline))

    health = client.get("/dashboard/api/health")
    snapshot = client.get("/dashboard/api/snapshot")

    assert health.status_code == 503
    assert snapshot.status_code == 503
    assert health.json()["storage"] == "unavailable"
    assert snapshot.json()["detail"] == "Dashboard data is temporarily unavailable."
    assert "do-not-leak" not in health.text + snapshot.text
    assert "+12025550147" not in health.text + snapshot.text


def test_timeline_endpoint_validates_event_ids_and_returns_safe_projection() -> None:
    events, timeline = example_data()
    client = build_client(FakeDashboardStore(events, timeline))

    response = client.get(f"/dashboard/api/events/{events[0].event_id}/timeline")
    missing = client.get("/dashboard/api/events/evt_missing/timeline")
    malformed = client.get("/dashboard/api/events/not-an-event/timeline")

    assert response.status_code == 200
    assert response.json()["timeline"][0]["from_state"] == "queued"
    assert "+12025550147" not in response.text
    assert missing.status_code == 404
    assert malformed.status_code == 422


def test_configuration_limits_are_validated() -> None:
    events, timeline = example_data()
    store = FakeDashboardStore(events, timeline)

    try:
        create_dashboard_router(store, prefix="/Dashboard")
    except ValueError as exc:
        assert "prefix" in str(exc)
    else:
        raise AssertionError("uppercase prefixes must be rejected")

    try:
        create_dashboard_router(store, event_limit=0)
    except ValueError as exc:
        assert "event_limit" in str(exc)
    else:
        raise AssertionError("invalid limits must be rejected")
