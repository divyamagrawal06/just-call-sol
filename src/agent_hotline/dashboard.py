"""Local, read-only operational dashboard for Agent Hotline.

The dashboard deliberately exposes a projection of durable data rather than
serializing domain models.  That keeps phone numbers, credentials, evidence,
workspace paths, thread identifiers, and provider payloads out of both the HTML
and JSON responses.
"""

from __future__ import annotations

import asyncio
import html
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Request
from fastapi import Path as ApiPath
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from .models import EscalationEvent, EventState, TimelineEntry
from .security import redact_text

_ASSET_DIRECTORY = Path(__file__).with_name("dashboard_assets")
_EVENT_ID_PATTERN = r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{2,124}$"
_PREFIX_PATTERN = re.compile(r"^/[a-z0-9][a-z0-9/_-]*$")
_PHONE_LIKE_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:\+?\d[\s().-]*){8,15}(?![A-Za-z0-9])")
_SAFE_DETAIL_KEYS = frozenset(
    {
        "failure_reason",
        "message",
        "note",
        "outcome",
        "provider_status",
        "reason",
        "result",
        "status",
    }
)
_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'none'; frame-ancestors 'none'; "
        "img-src 'self'; object-src 'none'; script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Expires": "0",
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class DashboardStore(Protocol):
    """The small, read-only storage surface consumed by the dashboard."""

    async def list_events(
        self,
        *,
        limit: int = 50,
        states: Iterable[EventState] | None = None,
    ) -> list[EscalationEvent]: ...

    async def get_event(self, event_id: str) -> EscalationEvent | None: ...

    async def list_timeline(
        self,
        *,
        event_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[TimelineEntry]: ...


def _secure(response: Response) -> Response:
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _is_local_request(request: Request) -> bool:
    """Require both the peer and requested hostname to be local.

    Checking the URL hostname as well as the socket peer prevents a local
    reverse proxy or tunnel from making the dashboard public accidentally.
    """

    if request.client is None:
        return False
    return request.client.host in _LOCAL_HOSTS and request.url.hostname in _LOCAL_HOSTS


def _local_only_error() -> Response:
    return _secure(
        JSONResponse(
            {"detail": "The operational dashboard is available on localhost only."},
            status_code=403,
        )
    )


def _safe_text(value: str, *, max_chars: int) -> str:
    redacted = redact_text(value, redact_phone_numbers=False)
    redacted = _PHONE_LIKE_PATTERN.sub("[REDACTED PHONE]", redacted)
    redacted = "".join(
        character for character in redacted if character in "\n\t" or ord(character) >= 32
    )
    if len(redacted) <= max_chars:
        return redacted
    return f"{redacted[: max_chars - 1]}…"


def _safe_timeline_detail(details: dict[str, Any]) -> dict[str, str | int | float | bool | None]:
    projection: dict[str, str | int | float | bool | None] = {}
    for raw_key, value in details.items():
        key = str(raw_key).strip().lower().replace("-", "_")
        if key not in _SAFE_DETAIL_KEYS or isinstance(value, (dict, list, tuple)):
            continue
        if isinstance(value, str):
            projection[key] = _safe_text(value, max_chars=240)
        elif value is None or isinstance(value, (bool, int, float)):
            projection[key] = value
    return projection


def _timeline_projection(entry: TimelineEntry) -> dict[str, Any]:
    return {
        "kind": entry.kind.value,
        "from_state": entry.from_state,
        "to_state": entry.to_state,
        "detail": _safe_timeline_detail(entry.details),
        "occurred_at": entry.occurred_at.isoformat(),
    }


def _event_projection(
    event: EscalationEvent,
    timeline: list[TimelineEntry],
) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "kind": event.kind.value,
        "severity": event.severity.value,
        "state": event.state.value,
        "source": event.source.value,
        "agent_type": event.agent_type.value,
        "summary": _safe_text(event.summary, max_chars=500),
        "blocking": event.blocking,
        "detected_at": event.detected_at.isoformat(),
        "deadline_at": event.deadline_at.isoformat() if event.deadline_at else None,
        "timeline": [_timeline_projection(entry) for entry in timeline],
    }


def _load_asset(name: str) -> str:
    return (_ASSET_DIRECTORY / name).read_text(encoding="utf-8")


def _validate_prefix(prefix: str) -> str:
    normalized = prefix.rstrip("/")
    if not _PREFIX_PATTERN.fullmatch(normalized):
        raise ValueError("dashboard prefix must be an absolute lowercase URL path")
    return normalized


def create_dashboard_router(
    store: DashboardStore,
    *,
    prefix: str = "/dashboard",
    event_limit: int = 30,
    timeline_limit: int = 12,
) -> APIRouter:
    """Build a localhost-only router containing exclusively GET endpoints."""

    prefix = _validate_prefix(prefix)
    if not 1 <= event_limit <= 100:
        raise ValueError("event_limit must be between 1 and 100")
    if not 1 <= timeline_limit <= 50:
        raise ValueError("timeline_limit must be between 1 and 50")

    router = APIRouter(prefix=prefix, tags=["local-dashboard"])

    @router.get("", include_in_schema=False)
    async def dashboard_redirect(request: Request) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        return _secure(RedirectResponse(f"{prefix}/", status_code=307))

    @router.get("/", include_in_schema=False)
    async def dashboard_index(request: Request) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        template = _load_asset("index.html")
        rendered = template.replace("__DASHBOARD_PREFIX__", html.escape(prefix, quote=True))
        return _secure(HTMLResponse(rendered))

    @router.get("/assets/dashboard.css", include_in_schema=False)
    async def dashboard_css(request: Request) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        return _secure(PlainTextResponse(_load_asset("dashboard.css"), media_type="text/css"))

    @router.get("/assets/dashboard.js", include_in_schema=False)
    async def dashboard_javascript(request: Request) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        return _secure(
            PlainTextResponse(
                _load_asset("dashboard.js"),
                media_type="application/javascript",
            )
        )

    @router.get("/api/health", include_in_schema=False)
    async def dashboard_health(request: Request) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        generated_at = datetime.now(UTC).isoformat()
        try:
            await store.list_events(limit=1)
        except Exception:
            return _secure(
                JSONResponse(
                    {
                        "status": "degraded",
                        "storage": "unavailable",
                        "read_only": True,
                        "generated_at": generated_at,
                    },
                    status_code=503,
                )
            )
        return _secure(
            JSONResponse(
                {
                    "status": "ok",
                    "storage": "connected",
                    "read_only": True,
                    "generated_at": generated_at,
                }
            )
        )

    @router.get("/api/snapshot", include_in_schema=False)
    async def dashboard_snapshot(request: Request) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        try:
            events = await store.list_events(limit=event_limit)
            timelines = await asyncio.gather(
                *(store.list_timeline(event_id=event.event_id, limit=200) for event in events)
            )
        except Exception:
            return _secure(
                JSONResponse(
                    {
                        "status": "degraded",
                        "detail": "Dashboard data is temporarily unavailable.",
                    },
                    status_code=503,
                )
            )

        counts = Counter(event.state.value for event in events)
        state_counts = {state.value: counts[state.value] for state in EventState}
        projections = [
            _event_projection(event, timeline[-timeline_limit:])
            for event, timeline in zip(events, timelines, strict=True)
        ]
        return _secure(
            JSONResponse(
                {
                    "status": "ok",
                    "read_only": True,
                    "generated_at": datetime.now(UTC).isoformat(),
                    "state_counts": state_counts,
                    "events": projections,
                }
            )
        )

    @router.get("/api/events/{event_id}/timeline", include_in_schema=False)
    async def dashboard_event_timeline(
        request: Request,
        event_id: Annotated[
            str,
            ApiPath(min_length=7, max_length=128, pattern=_EVENT_ID_PATTERN),
        ],
    ) -> Response:
        if not _is_local_request(request):
            return _local_only_error()
        try:
            event = await store.get_event(event_id)
            if event is None:
                return _secure(JSONResponse({"detail": "Event not found."}, status_code=404))
            timeline = await store.list_timeline(event_id=event_id, limit=200)
        except Exception:
            return _secure(
                JSONResponse(
                    {"detail": "Timeline data is temporarily unavailable."},
                    status_code=503,
                )
            )
        return _secure(
            JSONResponse(
                {
                    "event_id": event.event_id,
                    "timeline": [
                        _timeline_projection(entry) for entry in timeline[-timeline_limit:]
                    ],
                }
            )
        )

    return router


__all__ = ["DashboardStore", "create_dashboard_router"]
