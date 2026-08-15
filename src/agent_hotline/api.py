"""Authenticated FastAPI boundary for Agent Hotline."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi import Path as APIPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .codex_app_server import (
    CodexAppServerClient,
    SafeThreadController,
    open_codex_desktop_thread,
)
from .codex_bridge import VoiceApprovalHandler, register_voice_callbacks
from .contracts import (
    ContactHumanRequest,
    ContactHumanResult,
    EventSummary,
    FallbackDecisionRequest,
    FallbackDecisionResponse,
    FallbackOpenRequest,
    FallbackOpenResponse,
    NotifyHumanRequest,
    RepositoryContextQuery,
    RepositoryContextResponse,
)
from .coordinator import HotlineCoordinator
from .dashboard import create_dashboard_router
from .fallback_delivery import FallbackNotifier, create_fallback_notifier
from .openai_realtime import (
    IncomingCallError,
    OpenAIRealtimeError,
    OpenAIRealtimeManager,
    OpenAIWebhookVerificationError,
)
from .providers import CallProvider, create_call_provider
from .runbooks import RunbookRegistry, create_default_registry
from .settings import Settings, get_settings
from .storage import (
    ConflictError,
    NotFoundError,
    SQLiteStore,
    StorageError,
)
from .twilio import (
    TWILIO_MEDIA_STREAM_PATH,
    build_inbound_bridge_twiml,
    build_inbound_media_stream_twiml,
    build_outbound_bridge_twiml,
    build_outbound_media_stream_twiml,
    normalize_e164,
    validate_twilio_account_sid,
    validate_twilio_call_sid,
    verify_outbound_voice_event_signature,
    verify_status_event_signature,
    verify_twilio_webhook_signature,
    verify_twilio_websocket_signature,
)
from .twilio_media import TwilioMediaProtocolError, TwilioMediaStream
from .vapi import VapiAdapter
from .vobiz import (
    VOBIZ_HANGUP_CALLBACK_PATH,
    VOBIZ_INBOUND_VOICE_PATH,
    VOBIZ_OUTBOUND_VOICE_PATH,
    VOBIZ_RING_CALLBACK_PATH,
    build_inbound_bridge_xml,
    build_outbound_bridge_xml,
    validate_vobiz_auth_id,
    validate_vobiz_call_uuid,
    verify_answer_event_signature,
    verify_hangup_event_signature,
    verify_ring_event_signature,
    verify_vobiz_webhook_headers,
)
from .vobiz import (
    normalize_e164 as normalize_vobiz_e164,
)
from .watchdog import AgentFailureWatchdog

logger = logging.getLogger(__name__)
_MAX_BODY_BYTES = 64 * 1024
_VAPI_MAX_BODY_BYTES = 512 * 1024
_VAPI_WEBHOOK_PATH = "/v1/vapi/webhook"
_CODEX_THREAD_PREWARM_TIMEOUT_SECONDS = 60.0
_FALLBACK_ASSET_DIRECTORY = Path(__file__).with_name("fallback_assets")
_FALLBACK_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "font-src 'self'; form-action 'none'; frame-ancestors 'none'; "
        "img-src 'self'; object-src 'none'; script-src 'self'; style-src 'self'"
    ),
    "Cross-Origin-Resource-Policy": "same-origin",
    "Expires": "0",
    "Pragma": "no-cache",
}


class BodySizeLimitMiddleware:
    """Enforce a hard request-body bound for both fixed and chunked requests."""

    def __init__(
        self,
        app: ASGIApp,
        max_bytes: int = _MAX_BODY_BYTES,
        vapi_max_bytes: int = _VAPI_MAX_BODY_BYTES,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.vapi_max_bytes = vapi_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = (
            self.vapi_max_bytes
            if scope.get("path") == _VAPI_WEBHOOK_PATH
            else self.max_bytes
        )
        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > max_bytes:
                    await _send_too_large(send)
                    return
            except ValueError:
                await _send_too_large(send)
                return
        buffered: list[Message] = []
        received = 0
        more_body = True
        while more_body:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received += len(message.get("body", b""))
            if received > max_bytes:
                await _send_too_large(send)
                return
            more_body = bool(message.get("more_body", False))
        iterator = iter(buffered)

        async def replay_receive() -> Message:
            try:
                return next(iterator)
            except StopIteration:
                return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


class SlidingWindowLimiter:
    def __init__(self, *, requests: int = 120, window_seconds: float = 60.0) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            entries = self._entries[key]
            while entries and entries[0] < cutoff:
                entries.popleft()
            if len(entries) >= self.requests:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            entries.append(now)


async def _cancel_runtime_task(task: asyncio.Task[Any]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _cancel_runtime_tasks(tasks: set[asyncio.Task[Any]]) -> None:
    pending = tuple(tasks)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@asynccontextmanager
async def _runtime_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = cast(Settings, app.state.configured_settings)
    if (
        settings.hotline_transport == "openai_realtime"
        and not settings.openai_realtime_runtime_ready
    ):
        raise RuntimeError(
            "OpenAI Realtime transport requires complete OpenAI, carrier, public URL, "
            "owner, PIN, local-token, and independent signing-secret configuration"
        )
    settings.ensure_runtime_directory()
    async with contextlib.AsyncExitStack() as cleanup:
        store = SQLiteStore(settings.hotline_database_path)
        cleanup.push_async_callback(store.close)
        await store.initialize()

        provider = create_call_provider(settings)
        cleanup.push_async_callback(provider.close)
        fallback_notifier = create_fallback_notifier(settings)
        if fallback_notifier is not None:
            cleanup.push_async_callback(fallback_notifier.close)
        runbooks = create_default_registry(
            allow_real_execution=settings.hotline_allow_real_runbooks
        )
        codex: CodexAppServerClient | None = None
        controller: SafeThreadController | None = None

        if settings.codex_app_server_enabled:
            configured_roots = tuple(root.resolve() for root in settings.workspace_roots)
            explicit_cwd = (
                settings.codex_app_server_cwd.resolve()
                if settings.codex_app_server_cwd is not None
                else None
            )
            process_cwd = explicit_cwd or (
                configured_roots[0] if configured_roots else Path.cwd().resolve()
            )
            codex = CodexAppServerClient(
                codex_executable=settings.codex_bin,
                process_cwd=process_cwd,
            )
            cleanup.push_async_callback(codex.close)
            controller_roots = configured_roots or ((explicit_cwd,) if explicit_cwd else ())
            if controller_roots:
                controller_kwargs: dict[str, Any] = {
                    "workspace_roots": list(controller_roots)
                }
                if settings.hotline_show_spawned_codex_tasks:
                    controller_kwargs["spawned_thread_opener"] = open_codex_desktop_thread
                controller = SafeThreadController(
                    codex,
                    **controller_kwargs,
                )
            else:
                logger.warning(
                    "Codex task control disabled: configure HOTLINE_WORKSPACE_ROOTS or "
                    "CODEX_APP_SERVER_CWD"
                )

        coordinator = HotlineCoordinator(
            settings=settings,
            store=store,
            provider=provider,
            runbooks=runbooks,
            controller=controller,
            fallback_notifier=fallback_notifier,
        )
        realtime_manager: OpenAIRealtimeManager | None = None
        if settings.hotline_transport == "openai_realtime":
            realtime_manager = OpenAIRealtimeManager(
                settings=settings,
                store=store,
                coordinator=coordinator,
            )
            cleanup.push_async_callback(realtime_manager.close)

        escalation_tasks: set[asyncio.Task[ContactHumanResult]] = set()
        cleanup.push_async_callback(_cancel_runtime_tasks, escalation_tasks)
        if codex is not None:
            register_voice_callbacks(codex, VoiceApprovalHandler(coordinator))
            try:
                await codex.start()
            except Exception as exc:
                logger.warning(
                    "Codex App Server integration unavailable: %s",
                    type(exc).__name__,
                )
            else:
                monitor_task = asyncio.create_task(
                    _monitor_codex(codex, coordinator, escalation_tasks),
                    name="agent-hotline-codex-watchdog",
                )
                cleanup.push_async_callback(_cancel_runtime_task, monitor_task)
                if controller is not None:
                    try:
                        await controller.prewarm_voice_candidates(
                            timeout_seconds=_CODEX_THREAD_PREWARM_TIMEOUT_SECONDS
                        )
                    except Exception as exc:
                        logger.warning(
                            "Codex task prewarm unavailable: %s",
                            type(exc).__name__,
                        )

        if realtime_manager is not None:
            await realtime_manager.recover_active_calls()
        maintenance_task = asyncio.create_task(
            _maintain_expiring_state(coordinator),
            name="agent-hotline-expiring-state-maintenance",
        )
        cleanup.push_async_callback(_cancel_runtime_task, maintenance_task)
        if realtime_manager is not None:
            termination_maintenance_task = asyncio.create_task(
                _maintain_call_terminations(realtime_manager),
                name="agent-hotline-call-termination-maintenance",
            )
            cleanup.push_async_callback(
                _cancel_runtime_task,
                termination_maintenance_task,
            )

        app.state.settings = settings
        app.state.store = store
        app.state.provider = provider
        app.state.runbooks = runbooks
        app.state.codex = codex
        app.state.coordinator = coordinator
        app.state.realtime_manager = realtime_manager
        app.state.vapi_adapter = VapiAdapter(
            settings=settings,
            store=store,
            coordinator=coordinator,
        )
        yield


def create_app(
    *,
    settings: Settings | None = None,
    store: SQLiteStore | None = None,
    provider: CallProvider | None = None,
    runbooks: RunbookRegistry | None = None,
    controller: SafeThreadController | None = None,
    fallback_notifier: FallbackNotifier | None = None,
    realtime_manager: OpenAIRealtimeManager | None = None,
) -> FastAPI:
    """Create the production app or an injected, deterministic test app."""

    configured = settings or get_settings()
    injected = any(
        item is not None
        for item in (
            store,
            provider,
            runbooks,
            controller,
            fallback_notifier,
            realtime_manager,
        )
    )
    expose_development_docs = (
        configured.hotline_env != "production" and configured.public_base_url is None
    )
    lifespan = (
        _injected_lifespan(
            configured,
            store=store,
            provider=provider,
            runbooks=runbooks,
            controller=controller,
            fallback_notifier=fallback_notifier,
            realtime_manager=realtime_manager,
        )
        if injected
        else _runtime_lifespan
    )
    app = FastAPI(
        title="Agent Hotline",
        version=__version__,
        docs_url="/docs" if expose_development_docs else None,
        openapi_url="/openapi.json" if expose_development_docs else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.configured_settings = configured
    app.state.public_limiter = SlidingWindowLimiter()
    app.state.fallback_limiter = SlidingWindowLimiter(
        requests=10,
        window_seconds=60.0,
    )
    app.add_middleware(BodySizeLimitMiddleware)
    _install_error_handlers(app)
    _install_security_headers(app)
    _install_routes(app)
    app.include_router(create_dashboard_router(_DashboardStoreProxy(app)))
    return app


def _injected_lifespan(
    settings: Settings,
    *,
    store: SQLiteStore | None,
    provider: CallProvider | None,
    runbooks: RunbookRegistry | None,
    controller: SafeThreadController | None,
    fallback_notifier: FallbackNotifier | None,
    realtime_manager: OpenAIRealtimeManager | None,
) -> Callable[[FastAPI], Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with contextlib.AsyncExitStack() as cleanup:
            resolved_store = store or SQLiteStore(settings.hotline_database_path)
            cleanup.push_async_callback(resolved_store.close)
            await resolved_store.initialize()
            resolved_provider = provider or create_call_provider(settings)
            cleanup.push_async_callback(resolved_provider.close)
            if fallback_notifier is not None:
                cleanup.push_async_callback(fallback_notifier.close)
            resolved_runbooks = runbooks or create_default_registry(
                allow_real_execution=settings.hotline_allow_real_runbooks
            )
            coordinator = HotlineCoordinator(
                settings=settings,
                store=resolved_store,
                provider=resolved_provider,
                runbooks=resolved_runbooks,
                controller=controller,
                fallback_notifier=fallback_notifier,
            )
            resolved_realtime_manager = realtime_manager
            if (
                resolved_realtime_manager is None
                and settings.hotline_transport == "openai_realtime"
            ):
                resolved_realtime_manager = OpenAIRealtimeManager(
                    settings=settings,
                    store=resolved_store,
                    coordinator=coordinator,
                )
            if resolved_realtime_manager is not None:
                cleanup.push_async_callback(resolved_realtime_manager.close)
            recover_calls = getattr(resolved_realtime_manager, "recover_active_calls", None)
            reconcile_terminations = getattr(
                resolved_realtime_manager,
                "reconcile_pending_call_terminations",
                None,
            )
            if callable(recover_calls):
                await recover_calls()
            maintenance_task = asyncio.create_task(
                _maintain_expiring_state(coordinator),
                name="agent-hotline-expiring-state-maintenance",
            )
            cleanup.push_async_callback(_cancel_runtime_task, maintenance_task)
            if callable(reconcile_terminations):
                termination_maintenance_task = asyncio.create_task(
                    _maintain_call_terminations(resolved_realtime_manager),
                    name="agent-hotline-call-termination-maintenance",
                )
                cleanup.push_async_callback(
                    _cancel_runtime_task,
                    termination_maintenance_task,
                )
            app.state.settings = settings
            app.state.store = resolved_store
            app.state.provider = resolved_provider
            app.state.runbooks = resolved_runbooks
            app.state.codex = None
            app.state.coordinator = coordinator
            app.state.realtime_manager = resolved_realtime_manager
            app.state.vapi_adapter = VapiAdapter(
                settings=settings,
                store=resolved_store,
                coordinator=coordinator,
            )
            yield

    return lifespan


def _install_routes(app: FastAPI) -> None:
    @app.get("/")
    async def root() -> dict[str, str]:
        return {
            "service": "agent-hotline",
            "status": "running",
            "health": "/health",
        }

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        settings = _settings(request)
        store = _store(request)
        realtime_manager = cast(
            OpenAIRealtimeManager | None,
            getattr(request.app.state, "realtime_manager", None),
        )
        codex = cast(CodexAppServerClient | None, request.app.state.codex)
        if codex is not None:
            status = await codex.status()
            codex_status = {
                "state": status.state,
                "running": status.running,
                "initialized": status.initialized,
                "restart_count": status.restart_count,
            }
        else:
            codex_status = None
        controller = _coordinator(request).controller
        return {
            "status": "ok",
            "version": __version__,
            "database": str(await store.pragma("journal_mode")),
            "transport": settings.hotline_transport,
            "carrier": settings.hotline_carrier,
            "openai_realtime_configured": settings.openai_realtime_configured,
            "openai_realtime_runtime_ready": settings.openai_realtime_runtime_ready,
            "twilio_configured": settings.twilio_configured,
            "twilio_bridge_mode": settings.twilio_bridge_mode,
            "vobiz_configured": settings.vobiz_configured,
            "vapi_configured": settings.vapi_configured,
            "active_realtime_calls": (
                realtime_manager.active_calls if realtime_manager is not None else 0
            ),
            "secure_fallback_configured": settings.secure_fallback_configured,
            "codex_app_server": codex_status,
            "codex_spawn_navigation": (
                controller.spawn_navigation_status() if controller is not None else None
            ),
        }

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        await _store(request).pragma("journal_mode")
        settings = _settings(request)
        if settings.hotline_transport == "openai_realtime" and (
            not settings.openai_realtime_runtime_ready
            or getattr(request.app.state, "realtime_manager", None) is None
        ):
            raise HTTPException(status_code=503, detail="Realtime transport is not ready")
        return {"status": "ready"}

    @app.get("/fallback", include_in_schema=False)
    async def fallback_page() -> HTMLResponse:
        response = HTMLResponse(
            (_FALLBACK_ASSET_DIRECTORY / "index.html").read_text(encoding="utf-8")
        )
        response.headers.update(_FALLBACK_SECURITY_HEADERS)
        return response

    @app.get("/fallback/assets/fallback.css", include_in_schema=False)
    async def fallback_stylesheet() -> PlainTextResponse:
        response = PlainTextResponse(
            (_FALLBACK_ASSET_DIRECTORY / "fallback.css").read_text(encoding="utf-8"),
            media_type="text/css",
        )
        response.headers.update(_FALLBACK_SECURITY_HEADERS)
        return response

    @app.get("/fallback/assets/fallback.js", include_in_schema=False)
    async def fallback_javascript() -> PlainTextResponse:
        response = PlainTextResponse(
            (_FALLBACK_ASSET_DIRECTORY / "fallback.js").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )
        response.headers.update(_FALLBACK_SECURITY_HEADERS)
        return response

    @app.post(
        "/v1/fallback/open",
        response_model=FallbackOpenResponse,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_fallback)],
    )
    async def open_fallback(
        payload: FallbackOpenRequest,
        request: Request,
    ) -> FallbackOpenResponse:
        return await _coordinator(request).open_secure_fallback(payload)

    @app.post(
        "/v1/fallback/decision",
        response_model=FallbackDecisionResponse,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_fallback)],
    )
    async def decide_fallback(
        payload: FallbackDecisionRequest,
        request: Request,
    ) -> FallbackDecisionResponse:
        return await _coordinator(request).record_secure_fallback_decision(payload)

    @app.post(
        "/v1/escalations/contact",
        response_model=ContactHumanResult,
        dependencies=[Depends(_require_local_token)],
    )
    async def contact(
        payload: ContactHumanRequest,
        request: Request,
    ) -> ContactHumanResult:
        return await _coordinator(request).contact_human(payload)

    @app.post(
        "/v1/escalations/start",
        response_model=ContactHumanResult,
        dependencies=[Depends(_require_local_token)],
    )
    async def start_contact(
        payload: ContactHumanRequest,
        request: Request,
    ) -> ContactHumanResult:
        return await _coordinator(request).start_contact_human(payload)

    @app.post(
        "/v1/escalations/notify",
        response_model=ContactHumanResult,
        dependencies=[Depends(_require_local_token)],
    )
    async def notify(
        payload: NotifyHumanRequest,
        request: Request,
    ) -> ContactHumanResult:
        return await _coordinator(request).contact_human(payload)

    @app.get(
        "/v1/events",
        response_model=list[EventSummary],
        dependencies=[Depends(_require_local_token)],
    )
    async def list_events(
        request: Request,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[EventSummary]:
        return await _coordinator(request).list_events(limit)

    @app.get(
        "/v1/events/{event_id}",
        dependencies=[Depends(_require_local_token)],
    )
    async def get_event(
        request: Request,
        event_id: str = APIPath(min_length=7, max_length=128),
    ) -> dict[str, Any]:
        return await _coordinator(request).get_event_detail(event_id)

    @app.get(
        "/v1/events/{event_id}/result",
        response_model=ContactHumanResult,
        dependencies=[Depends(_require_local_token)],
    )
    async def get_event_result(
        request: Request,
        event_id: str = APIPath(min_length=7, max_length=128),
    ) -> ContactHumanResult:
        return await _coordinator(request).get_result(event_id)

    @app.post(
        "/v1/repository/context",
        response_model=RepositoryContextResponse,
        dependencies=[Depends(_require_local_token)],
    )
    async def local_repository_context(
        payload: RepositoryContextQuery,
        request: Request,
    ) -> RepositoryContextResponse:
        return await _coordinator(request).query_repository(payload)

    @app.post(
        "/v1/openai/realtime/webhook",
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def openai_realtime_webhook(request: Request) -> dict[str, Any]:
        manager = _realtime_manager(request)
        raw_body = await request.body()
        try:
            result = await manager.handle_webhook(
                raw_body,
                dict(request.headers.items()),
            )
        except OpenAIWebhookVerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except OpenAIRealtimeError as exc:
            logger.warning(
                "OpenAI Realtime webhook failed error_type=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail="Realtime call admission is temporarily unavailable",
            ) from exc
        return asdict(result)

    @app.post(
        _VAPI_WEBHOOK_PATH,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public), Depends(_require_vapi_token)],
    )
    async def vapi_webhook(request: Request) -> dict[str, Any]:
        """Receive Vapi tool calls and terminal call events at one endpoint."""

        try:
            payload = await request.json()
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Vapi requires HTTP 200 tool responses even when the payload cannot
            # be acted upon; an empty result prevents a retry storm.
            return {"accepted": False, "results": []}
        return await _vapi_adapter(request).handle(payload)

    @app.post(
        VOBIZ_OUTBOUND_VOICE_PATH,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def vobiz_outbound_voice(request: Request) -> Response:
        settings = _settings(request)
        async with _verified_vobiz_form(request, settings) as callback:
            _pairs, parameters, _receipt_id, processed = callback
            event_id, event_signature = _required_vobiz_event_binding(request)
            if not verify_answer_event_signature(
                settings.hotline_sip_correlation_secret,
                event_id=event_id,
                signature=event_signature,
            ):
                raise HTTPException(status_code=403, detail="Invalid callback correlation")
            call_uuid = _vobiz_outbound_call_uuid(parameters)
            _verify_vobiz_account(parameters, settings)
            _verify_vobiz_call_identity(
                parameters,
                settings,
                direction="outbound",
            )
            event = _single_vobiz_form_value(parameters, "Event", maximum=50)
            status = _vobiz_call_status(parameters)
            if event != "StartApp" or status != "in-progress":
                raise HTTPException(status_code=400, detail="Invalid Vobiz answer event")
            if settings.openai_project_id is None:
                raise HTTPException(status_code=503, detail="OpenAI project is not configured")
            try:
                if not processed:
                    _session, should_dial = await _realtime_manager(
                        request
                    ).bind_outbound_carrier_parent(
                        event_id=event_id,
                        call_sid=call_uuid,
                    )
                    if not should_dial:
                        return Response(
                            content=_empty_vobiz_xml(),
                            media_type="application/xml",
                        )
                xml = build_outbound_bridge_xml(
                    openai_project_id=settings.openai_project_id,
                    event_id=event_id,
                    correlation_secret=settings.hotline_sip_correlation_secret,
                    correlation_call_uuid=call_uuid,
                    max_call_duration_seconds=settings.hotline_max_call_duration_seconds,
                    dial_timeout_seconds=settings.hotline_outbound_ring_timeout_seconds,
                )
            except (OpenAIRealtimeError, NotFoundError, ValueError) as exc:
                logger.warning(
                    "Outbound Vobiz XML correlation failed error_type=%s",
                    type(exc).__name__,
                )
                raise HTTPException(
                    status_code=409,
                    detail="Outbound call could not be correlated",
                ) from exc
            return Response(content=xml, media_type="application/xml")

    @app.post(
        VOBIZ_INBOUND_VOICE_PATH,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def vobiz_incoming_voice(request: Request) -> Response:
        settings = _settings(request)
        async with _verified_vobiz_form(request, settings) as callback:
            _pairs, parameters, _receipt_id, _processed = callback
            _reject_vobiz_event_binding(request)
            call_uuid = _vobiz_inbound_call_uuid(parameters)
            _verify_vobiz_account(parameters, settings)
            caller_phone = _verify_vobiz_call_identity(
                parameters,
                settings,
                direction="inbound",
            )
            event = _optional_single_vobiz_form_value(
                parameters,
                "Event",
                maximum=50,
            )
            if event is not None and event != "StartApp":
                raise HTTPException(status_code=400, detail="Invalid Vobiz answer event")
            status = _vobiz_call_status(parameters)
            if status not in {"ringing", "in-progress"}:
                raise HTTPException(status_code=400, detail="Invalid Vobiz answer event")
            if settings.openai_project_id is None:
                raise HTTPException(status_code=503, detail="OpenAI project is not configured")
            try:
                carrier_admission = await _store(request).issue_carrier_admission(
                    call_uuid,
                    caller_phone=caller_phone,
                    ttl_seconds=settings.hotline_carrier_admission_ttl_seconds,
                )
                xml = build_inbound_bridge_xml(
                    openai_project_id=settings.openai_project_id,
                    call_uuid=call_uuid,
                    caller_phone=caller_phone,
                    admission_nonce=carrier_admission.admission_nonce,
                    expires_at_epoch=int(carrier_admission.expires_at.timestamp()),
                    correlation_secret=settings.hotline_sip_correlation_secret,
                    max_call_duration_seconds=settings.hotline_max_call_duration_seconds,
                    dial_timeout_seconds=settings.hotline_outbound_ring_timeout_seconds,
                )
            except (ConflictError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid Vobiz call") from exc
            return Response(content=xml, media_type="application/xml")

    @app.post(
        VOBIZ_RING_CALLBACK_PATH,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def vobiz_ring_callback(request: Request) -> dict[str, Any]:
        settings = _settings(request)
        async with _verified_vobiz_form(request, settings) as callback:
            _pairs, parameters, receipt_id, processed = callback
            event_id, event_signature = _required_vobiz_event_binding(request)
            if not verify_ring_event_signature(
                settings.hotline_sip_correlation_secret,
                event_id=event_id,
                signature=event_signature,
            ):
                raise HTTPException(status_code=403, detail="Invalid callback correlation")
            call_uuid = _vobiz_outbound_call_uuid(parameters)
            _verify_vobiz_account(parameters, settings)
            _verify_vobiz_call_identity(
                parameters,
                settings,
                direction="outbound",
            )
            event = _single_vobiz_form_value(parameters, "Event", maximum=50)
            status = _vobiz_call_status(parameters)
            if event != "Ring" or status != "ringing":
                raise HTTPException(status_code=400, detail="Invalid Vobiz ring event")
            if processed:
                return {"accepted": True, "terminal": False}
            return await _reconcile_vobiz_status(
                request,
                call_uuid=call_uuid,
                status=status,
                receipt_id=receipt_id,
                event_id=event_id,
            )

    @app.post(
        VOBIZ_HANGUP_CALLBACK_PATH,
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def vobiz_hangup_callback(request: Request) -> dict[str, Any]:
        settings = _settings(request)
        async with _verified_vobiz_form(request, settings) as callback:
            _pairs, parameters, receipt_id, processed = callback
            direction = _single_vobiz_form_value(
                parameters,
                "Direction",
                maximum=32,
            ).casefold()
            event_id: str | None = None
            if direction == "outbound":
                event_id, event_signature = _required_vobiz_event_binding(request)
                if not verify_hangup_event_signature(
                    settings.hotline_sip_correlation_secret,
                    event_id=event_id,
                    signature=event_signature,
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="Invalid callback correlation",
                    )
                call_uuid = _vobiz_outbound_call_uuid(parameters)
            elif direction == "inbound":
                _reject_vobiz_event_binding(request)
                call_uuid = _vobiz_inbound_call_uuid(parameters)
            else:
                raise HTTPException(status_code=400, detail="Invalid Vobiz direction")
            _verify_vobiz_account(parameters, settings)
            _verify_vobiz_call_identity(
                parameters,
                settings,
                direction=cast(Any, direction),
            )
            event = _optional_single_vobiz_form_value(
                parameters,
                "Event",
                maximum=50,
            )
            if (direction == "outbound" and event != "Hangup") or (
                direction == "inbound" and event not in {None, "Hangup"}
            ):
                raise HTTPException(status_code=400, detail="Invalid Vobiz hangup event")
            status = _vobiz_call_status(parameters)
            if status not in {
                "completed",
                "busy",
                "no-answer",
                "failed",
                "cancel",
                "canceled",
                "cancelled",
                "timeout",
            }:
                raise HTTPException(status_code=400, detail="Invalid Vobiz hangup status")
            if processed:
                return {"accepted": True, "terminal": False}
            return await _reconcile_vobiz_status(
                request,
                call_uuid=call_uuid,
                status=status,
                receipt_id=receipt_id,
                event_id=event_id,
            )

    @app.post(
        "/v1/twilio/voice/outbound",
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def twilio_outbound_voice(request: Request) -> Response:
        settings = _settings(request)
        _pairs, parameters = await _verified_twilio_form(request, settings)
        event_values = request.query_params.getlist("event_id")
        signature_values = request.query_params.getlist("event_sig")
        if len(event_values) != 1 or len(signature_values) != 1:
            raise HTTPException(status_code=400, detail="Invalid callback correlation")
        event_id = event_values[0]
        if not verify_outbound_voice_event_signature(
            settings.hotline_sip_correlation_secret,
            event_id=event_id,
            signature=signature_values[0],
        ):
            raise HTTPException(status_code=403, detail="Invalid callback correlation")

        call_sid = _single_form_value(parameters, "CallSid", maximum=100)
        account_sid = _single_form_value(parameters, "AccountSid", maximum=100)
        from_number = _single_form_value(parameters, "From", maximum=32)
        to_number = _single_form_value(parameters, "To", maximum=32)
        direction = _single_form_value(parameters, "Direction", maximum=50)
        try:
            call_sid = validate_twilio_call_sid(call_sid)
            account_sid = validate_twilio_account_sid(account_sid)
            normalized_from = normalize_e164(from_number)
            normalized_to = normalize_e164(to_number)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid outbound call") from exc
        if (
            settings.twilio_account_sid is None
            or not _safe_equal(account_sid, settings.twilio_account_sid)
            or not _safe_equal(
                normalized_from,
                settings.twilio_phone_number or "",
            )
            or not _safe_equal(
                normalized_to,
                settings.owner_phone_number.get_secret_value(),
            )
            or direction != "outbound-api"
        ):
            raise HTTPException(status_code=403, detail="Twilio call is not authorized")
        if settings.openai_project_id is None:
            raise HTTPException(status_code=503, detail="OpenAI project is not configured")
        try:
            _session, should_dial = await _realtime_manager(request).bind_outbound_carrier_parent(
                event_id=event_id,
                call_sid=call_sid,
            )
            if not should_dial:
                return Response(
                    content='<?xml version="1.0" encoding="UTF-8"?><Response />',
                    media_type="application/xml",
                )
            if settings.twilio_bridge_mode == "media_stream":
                twiml = build_outbound_media_stream_twiml(
                    public_base_url=settings.public_base_url or "",
                    event_id=event_id,
                    correlation_secret=settings.hotline_sip_correlation_secret,
                    correlation_call_sid=call_sid,
                )
            else:
                twiml = build_outbound_bridge_twiml(
                    openai_project_id=settings.openai_project_id,
                    event_id=event_id,
                    correlation_secret=settings.hotline_sip_correlation_secret,
                    correlation_call_sid=call_sid,
                    max_call_duration_seconds=settings.hotline_max_call_duration_seconds,
                    dial_timeout_seconds=settings.hotline_outbound_ring_timeout_seconds,
                )
        except (OpenAIRealtimeError, NotFoundError, ValueError) as exc:
            logger.warning(
                "Outbound TwiML correlation failed error_type=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=409,
                detail="Outbound call could not be correlated",
            ) from exc
        return Response(content=twiml, media_type="application/xml")

    @app.websocket(TWILIO_MEDIA_STREAM_PATH)
    async def twilio_media_stream(websocket: WebSocket) -> None:
        settings = cast(Settings, websocket.app.state.settings)
        if settings.twilio_bridge_mode != "media_stream":
            await websocket.close(code=1008, reason="media bridge is disabled")
            return
        auth_token = settings.twilio_auth_token.get_secret_value()
        if not auth_token or not settings.public_base_url:
            await websocket.close(code=1008, reason="media verification unavailable")
            return
        external_url = (
            "wss://" + settings.public_base_url.removeprefix("https://") + websocket.url.path
        )
        raw_query = websocket.scope.get("query_string", b"")
        if raw_query:
            try:
                external_url += "?" + raw_query.decode("ascii")
            except UnicodeDecodeError:
                await websocket.close(code=1008, reason="invalid media query")
                return
        if not verify_twilio_websocket_signature(
            url=external_url,
            params=None,
            signature=websocket.headers.get("x-twilio-signature"),
            auth_token=auth_token,
        ):
            await websocket.close(code=1008, reason="invalid media signature")
            return
        await websocket.accept()
        stream: TwilioMediaStream | None = None
        try:
            stream = await TwilioMediaStream.initialize(
                websocket,
                expected_account_sid=settings.twilio_account_sid or "",
                correlation_secret=settings.hotline_sip_correlation_secret.get_secret_value(),
            )
            await _realtime_manager(websocket).run_media_stream(stream)
        except (IncomingCallError, OpenAIRealtimeError, TwilioMediaProtocolError) as exc:
            logger.warning(
                "Twilio media stream ended error_type=%s",
                type(exc).__name__,
            )
            if stream is None:
                await websocket.close(code=1008, reason="invalid media stream")
        finally:
            if stream is not None:
                await stream.close()

    @app.post(
        "/v1/twilio/voice/incoming",
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def twilio_incoming_voice(request: Request) -> Response:
        settings = _settings(request)
        pairs, parameters = await _verified_twilio_form(request, settings)
        del pairs
        call_sid = _single_form_value(parameters, "CallSid", maximum=100)
        account_sid = _single_form_value(parameters, "AccountSid", maximum=100)
        caller_phone_raw = _single_form_value(parameters, "From", maximum=32)
        destination_raw = _single_form_value(parameters, "To", maximum=32)
        direction = _single_form_value(parameters, "Direction", maximum=32)
        try:
            call_sid = validate_twilio_call_sid(call_sid)
            account_sid = validate_twilio_account_sid(account_sid)
            caller_phone = normalize_e164(caller_phone_raw)
            destination = normalize_e164(destination_raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Twilio call") from exc
        configured_account = settings.twilio_account_sid or ""
        configured_destination = settings.twilio_phone_number or ""
        if (
            not _safe_equal(account_sid, configured_account)
            or not _safe_equal(destination, configured_destination)
            or direction.casefold() != "inbound"
        ):
            raise HTTPException(status_code=403, detail="Twilio call is not authorized")
        allowed_callers = (
            settings.owner_phone_number.get_secret_value(),
            *settings.allowlisted_callers,
        )
        normalized_allowlist: list[str] = []
        for allowed in allowed_callers:
            try:
                normalized_allowlist.append(normalize_e164(allowed))
            except (TypeError, ValueError):
                continue
        if not any(_safe_equal(caller_phone, allowed) for allowed in normalized_allowlist):
            raise HTTPException(status_code=403, detail="Twilio call is not authorized")
        if settings.openai_project_id is None:
            raise HTTPException(status_code=503, detail="OpenAI project is not configured")
        try:
            if settings.twilio_bridge_mode == "media_stream":
                carrier_admission = await _store(request).issue_carrier_admission(
                    call_sid,
                    caller_phone=caller_phone,
                    ttl_seconds=settings.hotline_carrier_admission_ttl_seconds,
                )
                twiml = build_inbound_media_stream_twiml(
                    public_base_url=settings.public_base_url or "",
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                    admission_nonce=carrier_admission.admission_nonce,
                    expires_at_epoch=int(carrier_admission.expires_at.timestamp()),
                    correlation_secret=settings.hotline_sip_correlation_secret,
                )
            else:
                carrier_admission = await _store(request).issue_carrier_admission(
                    call_sid,
                    caller_phone=caller_phone,
                    ttl_seconds=settings.hotline_carrier_admission_ttl_seconds,
                )
                twiml = build_inbound_bridge_twiml(
                    openai_project_id=settings.openai_project_id,
                    call_sid=call_sid,
                    caller_phone=caller_phone,
                    admission_nonce=carrier_admission.admission_nonce,
                    expires_at_epoch=int(carrier_admission.expires_at.timestamp()),
                    public_base_url=settings.public_base_url or "",
                    correlation_secret=settings.hotline_sip_correlation_secret,
                    max_call_duration_seconds=settings.hotline_max_call_duration_seconds,
                    dial_timeout_seconds=settings.hotline_outbound_ring_timeout_seconds,
                )
        except (ConflictError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid Twilio call") from exc
        return Response(content=twiml, media_type="application/xml")

    @app.post(
        "/v1/twilio/status",
        include_in_schema=False,
        response_model=None,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def twilio_call_status(request: Request) -> Response | dict[str, Any]:
        settings = _settings(request)
        pairs, parameters = await _verified_twilio_form(request, settings)
        event_values = request.query_params.getlist("event_id")
        signature_values = request.query_params.getlist("event_sig")
        if len(event_values) > 1 or len(signature_values) > 1:
            raise HTTPException(status_code=400, detail="Invalid callback correlation")
        correlated_event_id = event_values[0] if event_values else None
        correlation_signature = signature_values[0] if signature_values else None
        if (correlated_event_id is None) is not (correlation_signature is None):
            raise HTTPException(status_code=400, detail="Invalid callback correlation")
        if correlated_event_id is not None and not verify_status_event_signature(
            settings.hotline_sip_correlation_secret,
            event_id=correlated_event_id,
            signature=correlation_signature,
        ):
            raise HTTPException(status_code=403, detail="Invalid callback correlation")
        try:
            call_sid = validate_twilio_call_sid(
                _single_form_value(parameters, "CallSid", maximum=100)
            )
            if "AccountSid" in parameters:
                account_sid = validate_twilio_account_sid(
                    _single_form_value(parameters, "AccountSid", maximum=100)
                )
                if not _safe_equal(account_sid, settings.twilio_account_sid or ""):
                    raise HTTPException(
                        status_code=403,
                        detail="Twilio call is not authorized",
                    )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Twilio call") from exc
        status_field = "DialCallStatus" if "DialCallStatus" in parameters else "CallStatus"
        status = _single_form_value(parameters, status_field, maximum=50)
        receipt_id = hashlib.sha256(
            json.dumps(
                pairs,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()[:48]
        try:
            status_arguments: dict[str, str | None] = {
                "call_sid": call_sid,
                "status": status,
                "receipt_id": receipt_id,
            }
            if correlated_event_id is not None:
                status_arguments["correlated_event_id"] = correlated_event_id
            result = await _realtime_manager(request).handle_carrier_status(
                **status_arguments,
            )
        except (OpenAIRealtimeError, ValueError) as exc:
            logger.warning(
                "Twilio status reconciliation failed error_type=%s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=409,
                detail="Carrier status could not be reconciled",
            ) from exc
        if status_field == "DialCallStatus":
            # A <Dial action> response is interpreted as the parent call's next
            # TwiML document. An empty response ends that leg cleanly.
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response />',
                media_type="application/xml",
            )
        return result


async def _require_local_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = _settings(request).hotline_local_token.get_secret_value()
    _verify_bearer(authorization, expected, unavailable_status=503)


async def _require_vapi_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    settings = _settings(request)
    if not settings.vapi_configured:
        raise HTTPException(
            status_code=503,
            detail="Vapi resource binding is not configured",
        )
    expected = settings.vapi_webhook_token.get_secret_value()
    _verify_bearer(authorization, expected, unavailable_status=503)


async def _rate_limit_public(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    limiter = cast(SlidingWindowLimiter, request.app.state.public_limiter)
    await limiter.check(f"{client}:{request.url.path}")


async def _rate_limit_fallback(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    limiter = cast(SlidingWindowLimiter, request.app.state.fallback_limiter)
    await limiter.check(f"{client}:fallback")


def _verify_bearer(
    authorization: str | None,
    expected: str,
    *,
    unavailable_status: int,
) -> None:
    if not expected:
        raise HTTPException(
            status_code=unavailable_status,
            detail="Service authentication is not configured",
        )
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if not _safe_equal(authorization[len(prefix) :], expected):
        raise HTTPException(status_code=403, detail="Invalid bearer token")


def _safe_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _coordinator(request: Request) -> HotlineCoordinator:
    return cast(HotlineCoordinator, request.app.state.coordinator)


def _vapi_adapter(request: Request) -> VapiAdapter:
    return cast(VapiAdapter, request.app.state.vapi_adapter)


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _store(request: Request) -> SQLiteStore:
    return cast(SQLiteStore, request.app.state.store)


def _realtime_manager(request: Request) -> OpenAIRealtimeManager:
    manager = cast(
        OpenAIRealtimeManager | None,
        getattr(request.app.state, "realtime_manager", None),
    )
    if manager is None:
        raise HTTPException(status_code=503, detail="Realtime transport is unavailable")
    return manager


@asynccontextmanager
async def _verified_vobiz_form(
    request: Request,
    settings: Settings,
) -> AsyncIterator[tuple[list[tuple[str, str]], dict[str, list[str]], str, bool]]:
    if settings.hotline_carrier != "vobiz":
        raise HTTPException(status_code=404, detail="Carrier route is not enabled")
    auth_token = settings.vobiz_auth_token.get_secret_value()
    if not auth_token or not settings.public_base_url:
        raise HTTPException(status_code=503, detail="Vobiz webhook verification is unavailable")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Vobiz webhook must be form encoded")
    raw_body = await request.body()
    try:
        encoded = raw_body.decode("utf-8")
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=100,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Vobiz webhook form is invalid") from exc
    if any(not name or len(name) > 100 or len(value) > 4000 for name, value in pairs):
        raise HTTPException(status_code=400, detail="Vobiz webhook form is invalid")

    external_url = f"{settings.public_base_url}{request.url.path}"
    raw_query = request.scope.get("query_string", b"")
    if raw_query:
        try:
            external_url += "?" + raw_query.decode("ascii")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Webhook query is invalid") from exc
    verification = verify_vobiz_webhook_headers(
        callback_url=external_url,
        auth_token=auth_token,
        signature_v3=request.headers.get("x-vobiz-signature-v3"),
        nonce_v3=request.headers.get("x-vobiz-signature-v3-nonce"),
        signature_v2=request.headers.get("x-vobiz-signature-v2"),
        nonce_v2=request.headers.get("x-vobiz-signature-v2-nonce"),
    )
    if verification is None:
        raise HTTPException(status_code=403, detail="Vobiz webhook signature is invalid")

    parameters: dict[str, list[str]] = {}
    for name, value in pairs:
        parameters.setdefault(name, []).append(value)
    canonical_pairs = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical_pairs.encode()).hexdigest()
    receipt_key = f"vobiz:{verification.version}:{verification.nonce}"
    receipt_id = hashlib.sha256(
        f"{request.url.path}\n{receipt_key}\n{canonical_pairs}".encode()
    ).hexdigest()[:48]
    try:
        claim = await _store(request).claim_ingress_receipt(
            receipt_key,
            kind=f"vobiz_v{verification.version}",
            subject_id=request.url.path,
            fingerprint=fingerprint,
        )
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail="Vobiz callback nonce was reused") from exc

    try:
        yield pairs, parameters, receipt_id, claim.processed
    except BaseException:
        if not claim.processed:
            await _store(request).release_ingress_receipt(receipt_key)
        raise
    else:
        if not claim.processed:
            await _store(request).complete_ingress_receipt(receipt_key)


def _required_vobiz_event_binding(request: Request) -> tuple[str, str]:
    event_values = request.query_params.getlist("event_id")
    signature_values = request.query_params.getlist("event_sig")
    if len(event_values) != 1 or len(signature_values) != 1:
        raise HTTPException(status_code=400, detail="Invalid callback correlation")
    if not event_values[0] or not signature_values[0]:
        raise HTTPException(status_code=400, detail="Invalid callback correlation")
    return event_values[0], signature_values[0]


def _reject_vobiz_event_binding(request: Request) -> None:
    if request.query_params.getlist("event_id") or request.query_params.getlist("event_sig"):
        raise HTTPException(status_code=400, detail="Invalid callback correlation")


def _single_vobiz_form_value(
    parameters: Mapping[str, list[str]],
    name: str,
    *,
    maximum: int,
) -> str:
    values = parameters.get(name, [])
    if len(values) != 1:
        raise HTTPException(status_code=400, detail=f"Vobiz {name} is missing or duplicated")
    value = values[0]
    if not value or len(value) > maximum or value != value.strip():
        raise HTTPException(status_code=400, detail=f"Vobiz {name} is invalid")
    return value


def _optional_single_vobiz_form_value(
    parameters: Mapping[str, list[str]],
    name: str,
    *,
    maximum: int,
) -> str | None:
    if name not in parameters:
        return None
    return _single_vobiz_form_value(parameters, name, maximum=maximum)


def _vobiz_outbound_call_uuid(parameters: Mapping[str, list[str]]) -> str:
    try:
        request_uuid = validate_vobiz_call_uuid(
            _single_vobiz_form_value(parameters, "RequestUUID", maximum=100)
        )
        call_uuid_raw = _optional_single_vobiz_form_value(
            parameters,
            "CallUUID",
            maximum=100,
        )
        if call_uuid_raw is not None:
            call_uuid = validate_vobiz_call_uuid(call_uuid_raw)
            if not _safe_equal(call_uuid, request_uuid):
                raise ValueError("Vobiz call identifiers do not match")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Vobiz call UUID") from exc
    return request_uuid


def _vobiz_inbound_call_uuid(parameters: Mapping[str, list[str]]) -> str:
    try:
        return validate_vobiz_call_uuid(
            _single_vobiz_form_value(parameters, "CallUUID", maximum=100)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Vobiz call UUID") from exc


def _verify_vobiz_account(parameters: Mapping[str, list[str]], settings: Settings) -> None:
    configured = validate_vobiz_auth_id(settings.vobiz_auth_id)
    supplied: list[str] = []
    for name in ("auth_id", "ParentAuthID"):
        value = _optional_single_vobiz_form_value(parameters, name, maximum=100)
        if value is not None:
            supplied.append(value)
    try:
        if supplied and any(
            not _safe_equal(validate_vobiz_auth_id(value), configured) for value in supplied
        ):
            raise ValueError("Vobiz account does not match")
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Vobiz account is not authorized") from exc


def _verify_vobiz_call_identity(
    parameters: Mapping[str, list[str]],
    settings: Settings,
    *,
    direction: Literal["inbound", "outbound"],
) -> str:
    try:
        supplied_direction = _single_vobiz_form_value(
            parameters,
            "Direction",
            maximum=32,
        ).casefold()
        from_number = normalize_vobiz_e164(
            _single_vobiz_form_value(parameters, "From", maximum=32)
        )
        to_number = normalize_vobiz_e164(
            _single_vobiz_form_value(parameters, "To", maximum=32)
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid Vobiz call identity") from exc
    if supplied_direction != direction:
        raise HTTPException(status_code=403, detail="Vobiz call is not authorized")
    configured_number = settings.vobiz_phone_number or ""
    if direction == "outbound":
        if not _safe_equal(from_number, configured_number) or not _safe_equal(
            to_number,
            settings.owner_phone_number.get_secret_value(),
        ):
            raise HTTPException(status_code=403, detail="Vobiz call is not authorized")
        return to_number

    if not _safe_equal(to_number, configured_number):
        raise HTTPException(status_code=403, detail="Vobiz call is not authorized")
    allowed_callers = (
        settings.owner_phone_number.get_secret_value(),
        *settings.allowlisted_callers,
    )
    normalized_allowlist: list[str] = []
    for allowed in allowed_callers:
        try:
            normalized_allowlist.append(normalize_vobiz_e164(allowed))
        except (TypeError, ValueError):
            continue
    if not any(_safe_equal(from_number, allowed) for allowed in normalized_allowlist):
        raise HTTPException(status_code=403, detail="Vobiz call is not authorized")
    return from_number


def _vobiz_call_status(parameters: Mapping[str, list[str]]) -> str:
    return _single_vobiz_form_value(parameters, "CallStatus", maximum=50).casefold()


def _empty_vobiz_xml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response />'


async def _reconcile_vobiz_status(
    request: Request,
    *,
    call_uuid: str,
    status: str,
    receipt_id: str,
    event_id: str | None,
) -> dict[str, Any]:
    try:
        arguments: dict[str, str | None] = {
            "call_sid": call_uuid,
            "status": status,
            "receipt_id": receipt_id,
        }
        if event_id is not None:
            arguments["correlated_event_id"] = event_id
        return await _realtime_manager(request).handle_carrier_status(**arguments)
    except (OpenAIRealtimeError, ValueError) as exc:
        logger.warning(
            "Vobiz status reconciliation failed error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=409,
            detail="Carrier status could not be reconciled",
        ) from exc


async def _verified_twilio_form(
    request: Request,
    settings: Settings,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    if settings.hotline_carrier != "twilio":
        raise HTTPException(status_code=404, detail="Carrier route is not enabled")
    auth_token = settings.twilio_auth_token.get_secret_value()
    if not auth_token or not settings.public_base_url:
        raise HTTPException(status_code=503, detail="Twilio webhook verification is unavailable")
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().casefold()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Twilio webhook must be form encoded")
    raw_body = await request.body()
    try:
        encoded = raw_body.decode("utf-8")
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=100,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Twilio webhook form is invalid") from exc
    external_url = f"{settings.public_base_url}{request.url.path}"
    raw_query = request.scope.get("query_string", b"")
    if raw_query:
        try:
            external_url += "?" + raw_query.decode("ascii")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Webhook query is invalid") from exc
    if not verify_twilio_webhook_signature(
        url=external_url,
        params=pairs,
        signature=request.headers.get("x-twilio-signature"),
        auth_token=auth_token,
    ):
        raise HTTPException(status_code=403, detail="Twilio webhook signature is invalid")
    parameters: dict[str, list[str]] = {}
    for name, value in pairs:
        parameters.setdefault(name, []).append(value)
    return pairs, parameters


def _single_form_value(
    parameters: Mapping[str, list[str]],
    name: str,
    *,
    maximum: int,
) -> str:
    values = parameters.get(name)
    if values is None or len(values) != 1:
        raise HTTPException(status_code=400, detail=f"Twilio {name} is missing or duplicated")
    value = values[0].strip()
    if not value or len(value) > maximum:
        raise HTTPException(status_code=400, detail=f"Twilio {name} is invalid")
    return value


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        errors = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "message": str(error.get("msg", "Invalid input"))[:300],
                "type": str(error.get("type", "validation_error")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.exception_handler(NotFoundError)
    async def not_found(_request: Request, _exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Resource not found"})

    @app.exception_handler(ConflictError)
    async def conflict(_request: Request, _exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "State conflict"})

    @app.exception_handler(PermissionError)
    async def forbidden(_request: Request, _exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": "Operation not authorized"})

    @app.exception_handler(ValueError)
    @app.exception_handler(ValidationError)
    async def invalid_operation(
        _request: Request,
        _exc: ValueError | ValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid operation"},
        )

    @app.exception_handler(StorageError)
    async def storage_error(_request: Request, exc: StorageError) -> JSONResponse:
        logger.warning("Storage rejected request: %s", type(exc).__name__)
        return JSONResponse(status_code=409, content={"detail": "State operation rejected"})


def _install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Any]],
    ) -> Any:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response


async def _monitor_codex(
    client: CodexAppServerClient,
    coordinator: HotlineCoordinator,
    tasks: set[asyncio.Task[ContactHumanResult]],
) -> None:
    watchdog = AgentFailureWatchdog()
    async with client.subscribe(max_queue=500) as subscription:
        async for notification in subscription:
            params = notification.params
            if not isinstance(params, Mapping):
                continue
            escalation = watchdog.evaluate_codex_notification(
                notification.method,
                params,
            )
            if escalation is None:
                continue
            task = asyncio.create_task(
                coordinator.contact_human(escalation),
                name="agent-hotline-watchdog-escalation",
            )
            tasks.add(task)
            task.add_done_callback(tasks.discard)


async def _maintain_expiring_state(coordinator: HotlineCoordinator) -> None:
    last_slow_maintenance = 0.0
    while True:
        try:
            await coordinator.expire_decision_deadlines()
        except Exception as exc:
            logger.warning("Decision-deadline maintenance failed: %s", type(exc).__name__)
        now = time.monotonic()
        if now - last_slow_maintenance >= 30:
            try:
                await coordinator.expire_secure_fallbacks()
            except Exception as exc:
                logger.warning("Fallback maintenance failed: %s", type(exc).__name__)
            try:
                await coordinator.expire_prepared_actions()
            except Exception as exc:
                logger.warning("Prepared-action maintenance failed: %s", type(exc).__name__)
            last_slow_maintenance = now
        await asyncio.sleep(1)


async def _maintain_call_terminations(manager: OpenAIRealtimeManager) -> None:
    while True:
        try:
            await manager.recover_orphaned_call_legs()
        except Exception as exc:
            logger.warning("Orphaned-call recovery failed: %s", type(exc).__name__)
        try:
            await manager.expire_overdue_calls()
        except Exception as exc:
            logger.warning("Call-lifetime maintenance failed: %s", type(exc).__name__)
        try:
            await manager.reconcile_pending_call_terminations()
        except Exception as exc:
            logger.warning("Call-termination maintenance failed: %s", type(exc).__name__)
        await asyncio.sleep(1)


async def _send_too_large(send: Send) -> None:
    body = b'{"detail":"Request body too large"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _DashboardStoreProxy:
    """Resolve the lifespan-owned store without creating a second connection."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def list_events(
        self,
        *,
        limit: int = 50,
        states: Iterable[Any] | None = None,
    ) -> list[Any]:
        return await cast(SQLiteStore, self.app.state.store).list_events(
            limit=limit,
            states=states,
        )

    async def get_event(self, event_id: str) -> Any:
        return await cast(SQLiteStore, self.app.state.store).get_event(event_id)

    async def list_timeline(
        self,
        *,
        event_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[Any]:
        return await cast(SQLiteStore, self.app.state.store).list_timeline(
            event_id=event_id,
            session_id=session_id,
            limit=limit,
        )
