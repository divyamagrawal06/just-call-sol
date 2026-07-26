"""Authenticated FastAPI boundary for Agent Hotline."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import Path as APIPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .codex_app_server import CodexAppServerClient, SafeThreadController
from .codex_bridge import VoiceApprovalHandler, register_voice_callbacks
from .contracts import (
    BeginInboundSessionRequest,
    BeginInboundSessionResponse,
    ConfirmActionRequest,
    ConfirmActionResponse,
    ContactHumanRequest,
    ContactHumanResult,
    EscalationContextRequest,
    EscalationContextResponse,
    EventSummary,
    ExecuteActionRequest,
    ExecuteActionResponse,
    FallbackDecisionRequest,
    FallbackDecisionResponse,
    FallbackOpenRequest,
    FallbackOpenResponse,
    NotifyHumanRequest,
    PrepareActionRequest,
    PrepareActionResponse,
    RecordInstructionRequest,
    RecordInstructionResponse,
    RepositoryContextQuery,
    RepositoryContextResponse,
    SarvamRepositoryContextRequest,
    ThreadInspectRequest,
    ThreadListRequest,
)
from .coordinator import HotlineCoordinator
from .dashboard import create_dashboard_router
from .fallback_delivery import FallbackNotifier, create_fallback_notifier
from .providers import CallProvider, create_call_provider
from .runbooks import RunbookRegistry, create_default_registry
from .sarvam import InstantOutboundWebhook
from .settings import Settings, get_settings
from .storage import (
    ConflictError,
    NotFoundError,
    SQLiteStore,
    StorageError,
)
from .watchdog import AgentFailureWatchdog

logger = logging.getLogger(__name__)
_MAX_BODY_BYTES = 64 * 1024
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

    def __init__(self, app: ASGIApp, max_bytes: int = _MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.max_bytes:
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
            if received > self.max_bytes:
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


@asynccontextmanager
async def _runtime_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = cast(Settings, app.state.configured_settings)
    settings.ensure_runtime_directory()
    store = SQLiteStore(settings.hotline_database_path)
    await store.initialize()
    provider = create_call_provider(settings)
    fallback_notifier = create_fallback_notifier(settings)
    runbooks = create_default_registry(allow_real_execution=settings.hotline_allow_real_actions)
    codex: CodexAppServerClient | None = None
    controller: SafeThreadController | None = None
    monitor_task: asyncio.Task[None] | None = None
    maintenance_task: asyncio.Task[None] | None = None
    escalation_tasks: set[asyncio.Task[ContactHumanResult]] = set()

    if settings.codex_app_server_enabled:
        process_cwd = (settings.codex_app_server_cwd or Path.cwd()).resolve()
        codex = CodexAppServerClient(
            codex_executable=settings.codex_bin,
            process_cwd=process_cwd,
        )
        controller = SafeThreadController(codex, workspace_roots=[process_cwd])

    coordinator = HotlineCoordinator(
        settings=settings,
        store=store,
        provider=provider,
        runbooks=runbooks,
        controller=controller,
        fallback_notifier=fallback_notifier,
    )
    maintenance_task = asyncio.create_task(
        _maintain_fallbacks(coordinator),
        name="agent-hotline-fallback-maintenance",
    )
    if codex is not None:
        register_voice_callbacks(codex, VoiceApprovalHandler(coordinator))
        try:
            await codex.start()
        except Exception as exc:
            logger.warning("Codex App Server integration unavailable: %s", type(exc).__name__)
        else:
            monitor_task = asyncio.create_task(
                _monitor_codex(codex, coordinator, escalation_tasks),
                name="agent-hotline-codex-watchdog",
            )

    app.state.settings = settings
    app.state.store = store
    app.state.provider = provider
    app.state.runbooks = runbooks
    app.state.codex = codex
    app.state.coordinator = coordinator
    try:
        yield
    finally:
        if monitor_task is not None:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
        if maintenance_task is not None:
            maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance_task
        for task in escalation_tasks:
            task.cancel()
        if escalation_tasks:
            await asyncio.gather(*escalation_tasks, return_exceptions=True)
        if codex is not None:
            await codex.close()
        if fallback_notifier is not None:
            await fallback_notifier.close()
        await provider.close()
        await store.close()


def create_app(
    *,
    settings: Settings | None = None,
    store: SQLiteStore | None = None,
    provider: CallProvider | None = None,
    runbooks: RunbookRegistry | None = None,
    controller: SafeThreadController | None = None,
    fallback_notifier: FallbackNotifier | None = None,
) -> FastAPI:
    """Create the production app or an injected, deterministic test app."""

    configured = settings or get_settings()
    injected = any(
        item is not None for item in (store, provider, runbooks, controller, fallback_notifier)
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
    app.state.repository_context_limiter = SlidingWindowLimiter(
        requests=6,
        window_seconds=60.0,
    )
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
) -> Callable[[FastAPI], Any]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved_store = store or SQLiteStore(settings.hotline_database_path)
        await resolved_store.initialize()
        resolved_provider = provider or create_call_provider(settings)
        resolved_runbooks = runbooks or create_default_registry()
        coordinator = HotlineCoordinator(
            settings=settings,
            store=resolved_store,
            provider=resolved_provider,
            runbooks=resolved_runbooks,
            controller=controller,
            fallback_notifier=fallback_notifier,
        )
        maintenance_task = asyncio.create_task(
            _maintain_fallbacks(coordinator),
            name="agent-hotline-fallback-maintenance",
        )
        app.state.settings = settings
        app.state.store = resolved_store
        app.state.provider = resolved_provider
        app.state.runbooks = resolved_runbooks
        app.state.codex = None
        app.state.coordinator = coordinator
        try:
            yield
        finally:
            maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance_task
            if fallback_notifier is not None:
                await fallback_notifier.close()
            await resolved_provider.close()
            await resolved_store.close()

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
        return {
            "status": "ok",
            "version": __version__,
            "database": str(await store.pragma("journal_mode")),
            "transport": settings.hotline_transport,
            "sarvam_configured": settings.sarvam_configured,
            "public_tools_configured": settings.public_tools_configured,
            "secure_fallback_configured": settings.secure_fallback_configured,
            "codex_app_server": codex_status,
        }

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        await _store(request).pragma("journal_mode")
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
        "/v1/sarvam/tools/context",
        response_model=EscalationContextResponse,
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def context_tool(
        payload: EscalationContextRequest,
        request: Request,
    ) -> EscalationContextResponse:
        return await _coordinator(request).escalation_context(payload)

    @app.post(
        "/v1/sarvam/tools/record-instruction",
        response_model=RecordInstructionResponse,
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def instruction_tool(
        payload: RecordInstructionRequest,
        request: Request,
    ) -> RecordInstructionResponse:
        return await _coordinator(request).record_instruction(payload)

    @app.post(
        "/v1/sarvam/tools/prepare-action",
        response_model=PrepareActionResponse,
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def prepare_action_tool(
        payload: PrepareActionRequest,
        request: Request,
    ) -> PrepareActionResponse:
        return await _coordinator(request).prepare_action(payload)

    @app.post(
        "/v1/sarvam/tools/confirm-action",
        response_model=ConfirmActionResponse,
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def confirm_action_tool(
        payload: ConfirmActionRequest,
        request: Request,
    ) -> ConfirmActionResponse:
        return await _coordinator(request).confirm_action(payload)

    @app.post(
        "/v1/sarvam/tools/execute-action",
        response_model=ExecuteActionResponse,
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def execute_action_tool(
        payload: ExecuteActionRequest,
        request: Request,
    ) -> ExecuteActionResponse:
        return await _coordinator(request).execute_action(payload)

    @app.post(
        "/v1/sarvam/tools/begin-inbound",
        response_model=BeginInboundSessionResponse,
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def begin_inbound_tool(
        payload: BeginInboundSessionRequest,
        request: Request,
    ) -> BeginInboundSessionResponse:
        return await _coordinator(request).begin_inbound_session(payload)

    @app.post(
        "/v1/sarvam/tools/threads/list",
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def list_threads_tool(
        payload: ThreadListRequest,
        request: Request,
    ) -> dict[str, Any]:
        return await _coordinator(request).list_threads(payload)

    @app.post(
        "/v1/sarvam/tools/threads/inspect",
        dependencies=[Depends(_require_tool_token), Depends(_rate_limit_public)],
    )
    async def inspect_thread_tool(
        payload: ThreadInspectRequest,
        request: Request,
    ) -> dict[str, Any]:
        return await _coordinator(request).inspect_thread(payload)

    @app.post(
        "/v1/sarvam/tools/repository-context",
        response_model=RepositoryContextResponse,
        dependencies=[
            Depends(_require_tool_token),
            Depends(_rate_limit_repository_context),
        ],
    )
    async def repository_context_tool(
        payload: SarvamRepositoryContextRequest,
        request: Request,
    ) -> RepositoryContextResponse:
        return await _coordinator(request).query_repository_for_voice(payload)

    @app.post(
        "/v1/sarvam/webhooks/instant-outbound/{callback_token}",
        include_in_schema=False,
        dependencies=[Depends(_rate_limit_public)],
    )
    async def outbound_webhook(
        payload: InstantOutboundWebhook,
        request: Request,
        callback_token: str = APIPath(min_length=16, max_length=200),
    ) -> dict[str, Any]:
        expected = _settings(request).hotline_callback_token.get_secret_value()
        if not _safe_equal(callback_token, expected):
            raise HTTPException(status_code=404, detail="Not found")
        return await _coordinator(request).reconcile_webhook(payload)


async def _require_local_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = _settings(request).hotline_local_token.get_secret_value()
    _verify_bearer(authorization, expected, unavailable_status=503)


async def _require_tool_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    expected = _settings(request).hotline_tool_token.get_secret_value()
    _verify_bearer(authorization, expected, unavailable_status=503)


async def _rate_limit_public(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    limiter = cast(SlidingWindowLimiter, request.app.state.public_limiter)
    await limiter.check(f"{client}:{request.url.path}")


async def _rate_limit_repository_context(request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    limiter = cast(SlidingWindowLimiter, request.app.state.repository_context_limiter)
    await limiter.check(f"{client}:repository-context")


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


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _store(request: Request) -> SQLiteStore:
    return cast(SQLiteStore, request.app.state.store)


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


async def _maintain_fallbacks(coordinator: HotlineCoordinator) -> None:
    while True:
        try:
            await coordinator.expire_secure_fallbacks()
        except Exception as exc:
            logger.warning("Fallback maintenance failed: %s", type(exc).__name__)
        await asyncio.sleep(30)


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
