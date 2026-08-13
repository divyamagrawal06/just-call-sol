from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
import pytest_asyncio
from openai import InvalidWebhookSignatureError
from openai.types.realtime import RealtimeSessionCreateRequestParam
from pydantic import SecretStr, TypeAdapter
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

import agent_hotline.openai_realtime as realtime
import agent_hotline.storage as storage_module
from agent_hotline.contracts import (
    ConfirmActionResponse,
    ExecuteActionResponse,
    PrepareActionResponse,
    RecordInstructionResponse,
    RepositoryContextItem,
    RepositoryContextResponse,
)
from agent_hotline.coordinator import HotlineCoordinator
from agent_hotline.models import (
    ContactDirection,
    ContactSession,
    EscalationEvent,
    EventState,
    SessionState,
    TimelineKind,
    utc_now,
)
from agent_hotline.openai_realtime import (
    OpenAIRealtimeAPIError,
    OpenAIRealtimeClient,
    OpenAIRealtimeError,
    OpenAIRealtimeManager,
    OpenAIWebhookVerificationError,
    RealtimeConversation,
    RealtimeToolDispatcher,
    ToolDispatchResult,
)
from agent_hotline.providers import FakeCallProvider
from agent_hotline.realtime_prompt import (
    build_realtime_session_config,
    realtime_tool_definitions,
)
from agent_hotline.runbooks import create_default_registry
from agent_hotline.settings import Settings
from agent_hotline.storage import (
    ActiveSessionError,
    ConflictError,
    SQLiteStore,
    StorageError,
)
from agent_hotline.telephony_security import build_correlation_signature

OPENAI_API_KEY = "sk-openai-realtime-test-secret"
OPENAI_WEBHOOK_SECRET = "whsec-openai-realtime-test-secret"
CALLBACK_TOKEN = "sip-correlation-signing-token-for-tests-123456"
OWNER_PIN = "246810"
OWNER_PHONE = "+12025550123"
ALLOWLISTED_PHONE = "+12025550124"
TWILIO_CALL_SID = "CA" + ("b" * 32)
TWILIO_CHILD_CALL_SID = "CA" + ("c" * 32)
TWILIO_ACCOUNT_SID = "AC" + ("a" * 32)


def configured_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "hotline_env": "test",
        "hotline_transport": "openai_realtime",
        "openai_api_key": SecretStr(OPENAI_API_KEY),
        "openai_webhook_secret": SecretStr(OPENAI_WEBHOOK_SECRET),
        "openai_project_id": "proj_hotline_test",
        "public_base_url": "https://hotline.example.test",
        "hotline_local_token": SecretStr("local-realtime-service-token-for-tests-123456"),
        "hotline_sip_correlation_secret": SecretStr(CALLBACK_TOKEN),
        "hotline_action_signing_secret": SecretStr(
            "action-signing-token-for-realtime-tests-123456"
        ),
        "hotline_fallback_signing_secret": SecretStr(
            "fallback-signing-token-for-realtime-tests-123456"
        ),
        "owner_confirmation_pin": SecretStr(OWNER_PIN),
        "owner_phone_number": SecretStr(OWNER_PHONE),
        "hotline_allowlisted_callers": ALLOWLISTED_PHONE,
        "twilio_account_sid": TWILIO_ACCOUNT_SID,
        "hotline_voice_pin_max_attempts": 2,
        "hotline_retry_attempts": 0,
        "codex_app_server_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest_asyncio.fixture
async def store() -> AsyncIterator[SQLiteStore]:
    database = SQLiteStore(":memory:")
    await database.initialize()
    try:
        yield database
    finally:
        await database.close()


def real_coordinator(
    settings: Settings,
    store: SQLiteStore,
    *,
    provider: FakeCallProvider | None = None,
) -> HotlineCoordinator:
    return HotlineCoordinator(
        settings=settings,
        store=store,
        provider=provider or FakeCallProvider(),
        runbooks=create_default_registry(),
    )


class SocketEnded(RuntimeError):
    pass


class FakeRealtimeSocket:
    def __init__(self, *, auto_ack_delivery: bool = True) -> None:
        self.incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self.sent_raw: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.auto_ack_delivery = auto_ack_delivery
        self.closed = False
        self.close_code: int | None = None
        self.close_reason = ""

    async def send(self, message: str) -> None:
        self.sent_raw.append(message)
        payload = json.loads(message)
        assert isinstance(payload, dict)
        self.sent.append(payload)
        if not self.auto_ack_delivery:
            return
        item = payload.get("item")
        if (
            payload.get("type") == "conversation.item.create"
            and isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and isinstance(item.get("id"), str)
        ):
            self.push(
                {
                    "type": "conversation.item.created",
                    "item": {"id": item["id"]},
                }
            )
        response = payload.get("response")
        metadata = response.get("metadata") if isinstance(response, dict) else None
        tool_call_id = metadata.get("hotline_tool_call_id") if isinstance(metadata, dict) else None
        if payload.get("type") == "response.create" and isinstance(tool_call_id, str):
            self.push(
                {
                    "type": "response.created",
                    "response": {
                        "metadata": {"hotline_tool_call_id": tool_call_id},
                    },
                }
            )

    async def recv(self) -> str | bytes:
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        self.close_reason = reason
        self.incoming.put_nowait(SocketEnded("fake socket closed"))

    def push(self, event: Mapping[str, Any] | str | bytes) -> None:
        if isinstance(event, Mapping):
            self.incoming.put_nowait(
                json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
            )
        else:
            self.incoming.put_nowait(event)


class FakeSocketFactory:
    def __init__(self, socket: FakeRealtimeSocket | None = None) -> None:
        self.socket = socket or FakeRealtimeSocket()
        self.connections: list[tuple[str, dict[str, str]]] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> Any:
        @asynccontextmanager
        async def connection() -> AsyncIterator[FakeRealtimeSocket]:
            self.connections.append((url, dict(headers)))
            try:
                yield self.socket
            finally:
                await self.socket.close()

        return connection()


class FakeControlClient:
    def __init__(self) -> None:
        self.unwrapped: list[tuple[bytes, dict[str, str]]] = []
        self.accepted: list[tuple[str, dict[str, Any]]] = []
        self.rejected: list[tuple[str, int]] = []
        self.hung_up: list[str] = []
        self.closed = False

    def unwrap_webhook(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> dict[str, Any]:
        copied_headers = dict(headers)
        self.unwrapped.append((raw_body, copied_headers))
        signature = next(
            (
                value
                for name, value in copied_headers.items()
                if name.casefold() == "x-test-signature"
            ),
            None,
        )
        if signature != "valid":
            raise OpenAIWebhookVerificationError("webhook signature is invalid")
        payload = json.loads(raw_body)
        assert isinstance(payload, dict)
        return payload

    async def accept_call(
        self,
        call_id: str,
        session: Mapping[str, Any],
    ) -> None:
        self.accepted.append(
            (
                call_id,
                json.loads(json.dumps(dict(session), ensure_ascii=False)),
            )
        )

    async def reject_call(self, call_id: str, *, status_code: int = 603) -> None:
        self.rejected.append((call_id, status_code))

    async def hangup_call(self, call_id: str) -> None:
        self.hung_up.append(call_id)

    async def close(self) -> None:
        self.closed = True


class FakeWebhookResource:
    def __init__(
        self,
        result: object,
        *,
        error: Exception | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def unwrap(self, body: str, headers: Mapping[str, str]) -> object:
        self.calls.append((body, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.result


class FakeWebhookClient:
    def __init__(self, resource: FakeWebhookResource) -> None:
        self.webhooks = resource
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DumpableWebhookEvent:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


class StubHTTPClient:
    def __init__(
        self,
        responses: list[httpx.Response] | None = None,
    ) -> None:
        self.responses = list(responses or [])
        self.requests: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.closed = False

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None,
    ) -> httpx.Response:
        self.requests.append((method, path, json))
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200)

    async def aclose(self) -> None:
        self.closed = True


class RecordingToolCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.evidence_exposed = False
        self.executed_actions: set[str] = set()

    async def record_instruction(self, request: object) -> RecordInstructionResponse:
        self.calls.append(("record_instruction", request))
        if self.evidence_exposed:
            raise PermissionError("repository evidence makes this call non-authoritative")
        return RecordInstructionResponse(
            accepted=True,
            event_id=cast(Any, request).event_id,
            decision_id="dec_realtime_test",
            message_to_user="Decision recorded.",
            message_to_agent="The owner decision was recorded.",
        )

    async def prepare_action(self, request: object) -> PrepareActionResponse:
        self.calls.append(("prepare_action", request))
        if self.evidence_exposed:
            raise PermissionError("repository evidence makes this call non-authoritative")
        return PrepareActionResponse(
            action_id="act_realtime_test",
            action_hash="a" * 64,
            confirmation_nonce="nonce_realtime_test",
            risk="high",
            exact_readback="Confirm exact production action.",
            expires_at=utc_now() + timedelta(minutes=2),
        )

    async def confirm_action(self, request: object) -> ConfirmActionResponse:
        self.calls.append(("confirm_action", request))
        if self.evidence_exposed:
            raise PermissionError("repository evidence makes this call non-authoritative")
        return ConfirmActionResponse(
            confirmed=True,
            action_id=cast(Any, request).action_id,
            grant_id="grt_realtime_test",
            expires_at=utc_now() + timedelta(minutes=1),
            message_to_user="Action confirmed.",
        )

    async def execute_action(self, request: object) -> ExecuteActionResponse:
        self.calls.append(("execute_action", request))
        action_id = cast(Any, request).action_id
        if action_id in self.executed_actions:
            raise PermissionError("one-time action grant was already consumed")
        self.executed_actions.add(action_id)
        return ExecuteActionResponse(
            executed=True,
            action_id=action_id,
            grant_id=cast(Any, request).grant_id,
            operation_id="op_realtime_test",
            message_to_user="Action executed.",
            result={"status": "ok"},
        )

    async def query_repository_for_voice(
        self,
        request: object,
    ) -> RepositoryContextResponse:
        self.calls.append(("query_repository_for_voice", request))
        self.evidence_exposed = True
        return RepositoryContextResponse(
            workspace="C:/workspace/project",
            workspace_ref="repo_ref",
            operation="status",
            summary="Repository evidence returned.",
            items=[
                RepositoryContextItem(
                    kind="status",
                    text="Working tree is clean.",
                )
            ],
        )


class StaticDispatcher:
    def __init__(
        self,
        result: ToolDispatchResult | None = None,
    ) -> None:
        self.result = result or ToolDispatchResult(payload={"ok": True, "value": "done"})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> ToolDispatchResult:
        self.calls.append((name, dict(arguments)))
        return self.result


async def wait_until(
    predicate: Callable[[], bool],
    *,
    within_seconds: float = 1.0,
) -> None:
    async with asyncio.timeout(within_seconds):
        while not predicate():
            next_turn = asyncio.Event()
            asyncio.get_running_loop().call_soon(next_turn.set)
            await next_turn.wait()


async def process_tool_call_with_server_acks(
    conversation: RealtimeConversation,
    socket: FakeRealtimeSocket,
    *,
    tool_call_id: str,
    name: str,
    arguments_json: str,
) -> None:
    """Run a direct tool call while emulating correlated server acknowledgements."""

    original_auto_ack = socket.auto_ack_delivery
    socket.auto_ack_delivery = False
    first_unseen = len(socket.sent)
    tool_task = asyncio.create_task(
        conversation._process_tool_call(
            socket,
            tool_call_id=tool_call_id,
            name=name,
            arguments_json=arguments_json,
        )
    )
    try:
        while not tool_task.done():
            while first_unseen < len(socket.sent):
                event = socket.sent[first_unseen]
                first_unseen += 1
                item = event.get("item")
                item_id = item.get("id") if isinstance(item, dict) else None
                if event.get("type") == "conversation.item.create" and isinstance(item_id, str):
                    waiter = conversation._item_ack_waiters.get(item_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
                response = event.get("response")
                metadata = response.get("metadata") if isinstance(response, dict) else None
                acknowledged_tool_call_id = (
                    metadata.get("hotline_tool_call_id") if isinstance(metadata, dict) else None
                )
                if event.get("type") == "response.create" and isinstance(
                    acknowledged_tool_call_id, str
                ):
                    waiter = conversation._response_ack_waiters.get(acknowledged_tool_call_id)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(None)
            await asyncio.sleep(0)
        await tool_task
    finally:
        socket.auto_ack_delivery = original_auto_ack
        if not tool_task.done():
            tool_task.cancel()
            await asyncio.gather(tool_task, return_exceptions=True)


def incoming_webhook(
    *,
    call_id: str,
    caller: str = OWNER_PHONE,
    event_id: str = "evt_webhook_incoming",
    extra_headers: Mapping[str, str] | None = None,
    carrier_correlated: bool = True,
    carrier_call_sid: str = TWILIO_CALL_SID,
    twilio_child_call_sid: str = TWILIO_CHILD_CALL_SID,
    admission_nonce: str = "admission_test_nonce_0123456789abcdef",
    expires_at_epoch: int | None = None,
) -> dict[str, Any]:
    sip_headers = [
        {"name": "From", "value": f"<sip:{caller}@carrier.example>"},
        {"name": "X-Twilio-AccountSid", "value": TWILIO_ACCOUNT_SID},
        {"name": "X-Twilio-CallSid", "value": twilio_child_call_sid},
    ]
    if carrier_correlated and extra_headers is None:
        expiry = expires_at_epoch or int((utc_now() + timedelta(minutes=5)).timestamp())
        inbound_signature = build_correlation_signature(
            CALLBACK_TOKEN,
            direction="inbound",
            call_sid=carrier_call_sid,
            caller_phone=caller,
            admission_nonce=admission_nonce,
            expires_at_epoch=expiry,
        )
        extra_headers = {
            "X-Hotline-Direction": "inbound",
            "X-Hotline-Call-Sid": carrier_call_sid,
            "X-Hotline-Caller": caller,
            "X-Hotline-Admission": admission_nonce,
            "X-Hotline-Expires": str(expiry),
            "X-Hotline-Signature": inbound_signature,
        }
    sip_headers.extend(
        {"name": name, "value": value} for name, value in (extra_headers or {}).items()
    )
    return {
        "id": event_id,
        "type": "realtime.call.incoming",
        "data": {
            "call_id": call_id,
            "sip_headers": sip_headers,
        },
    }


async def admitted_incoming_webhook(
    store: SQLiteStore,
    *,
    call_id: str,
    caller: str = OWNER_PHONE,
    event_id: str = "evt_webhook_incoming",
    carrier_call_sid: str = TWILIO_CALL_SID,
) -> dict[str, Any]:
    admission = await store.issue_carrier_admission(
        carrier_call_sid,
        caller_phone=caller,
        ttl_seconds=300,
    )
    return incoming_webhook(
        call_id=call_id,
        caller=caller,
        event_id=event_id,
        carrier_call_sid=carrier_call_sid,
        admission_nonce=admission.admission_nonce,
        expires_at_epoch=int(admission.expires_at.timestamp()),
    )


def webhook_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def verified_headers(webhook_id: str) -> dict[str, str]:
    return {
        "webhook-id": webhook_id,
        "x-test-signature": "valid",
    }


async def persist_recoverable_realtime_call(
    store: SQLiteStore,
    *,
    call_id: str,
    attempt_id: str = TWILIO_CALL_SID,
) -> tuple[EscalationEvent, ContactSession]:
    event = EscalationEvent(
        kind="status",
        summary="A durably correlated Realtime call survived a daemon restart.",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    await store.transition_event(event.event_id, EventState.CONNECTED)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.INBOUND_CONTROL,
            state=SessionState.CONNECTED,
            attempt_id=attempt_id,
            interaction_id=call_id,
            provider="openai_realtime",
        )
    )
    return event, session


def build_conversation(
    *,
    settings: Settings,
    store: SQLiteStore,
    client: FakeControlClient,
    socket_factory: FakeSocketFactory,
    coordinator: object,
    call_id: str = "call_realtime_test",
) -> RealtimeConversation:
    return RealtimeConversation(
        settings=settings,
        store=store,
        client=cast(Any, client),
        socket_factory=socket_factory,
        call_id=call_id,
        attempt_id=TWILIO_CALL_SID,
        event_id="evt_realtime_test",
        session_id="ses_realtime_test",
        direction="outbound_escalation",
        coordinator=cast(Any, coordinator),
    )


@pytest.mark.parametrize(
    ("direction", "opening_fragment"),
    [
        ("outbound_escalation", "it's your agent"),
        ("inbound_control", "you're talking directly to your agent"),
    ],
)
def test_session_config_is_complete_and_direction_specific(
    direction: realtime.Direction,
    opening_fragment: str,
) -> None:
    settings = configured_settings(
        hotline_owner_name="Test Owner",
        openai_realtime_reasoning_effort="low",
    )

    config = build_realtime_session_config(settings, direction=direction)

    assert config["type"] == "realtime"
    assert config["model"] == "gpt-realtime-2.1"
    assert config["reasoning"] == {"effort": "low"}
    assert config["output_modalities"] == ["audio"]
    assert config["audio"]["output"] == {"voice": "marin"}
    assert config["audio"]["input"]["turn_detection"] == {
        "type": "semantic_vad",
        "eagerness": "low",
        "create_response": True,
        "interrupt_response": True,
    }
    assert config["tool_choice"] == "auto"
    assert config["parallel_tool_calls"] is False
    assert config["max_output_tokens"] == 1400
    assert config["truncation"] == "auto"
    assert opening_fragment in config["instructions"]
    assert "Test Owner" in config["instructions"]
    assert "Never ask the owner to speak the PIN" in config["instructions"]
    assert config["tools"] == realtime_tool_definitions()
    assert TypeAdapter(RealtimeSessionCreateRequestParam).validate_python(config) == config


def test_tool_schemas_are_strict_call_bound_and_contain_no_authority_secrets() -> None:
    tools = realtime_tool_definitions()
    names = [tool["name"] for tool in tools]

    assert names == [
        "get_hotline_context",
        "list_available_actions",
        "prepare_decision",
        "arm_owner_verification",
        "check_owner_verification",
        "record_decision",
        "list_agent_tasks",
        "inspect_agent_task",
        "prepare_action",
        "confirm_action",
        "execute_action",
        "prepare_repository_access",
        "query_repository_context",
        "wait_for_user",
        "finish_session",
    ]
    assert len(names) == len(set(names))

    forbidden = {
        "event_id",
        "session_id",
        "call_id",
        "confirmation_pin",
        "pin",
        "api_key",
        "webhook_secret",
    }
    for tool in tools:
        assert tool["type"] == "function"
        parameters = tool["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) <= set(parameters["properties"])
        assert forbidden.isdisjoint(parameters["properties"])

    arm_tool = next(tool for tool in tools if tool["name"] == "arm_owner_verification")
    assert arm_tool["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_client_builds_secret_bearing_transport_and_exact_call_control_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructed: dict[str, Any] = {}
    http_client = StubHTTPClient()

    def make_http_client(**kwargs: Any) -> StubHTTPClient:
        constructed.update(kwargs)
        return http_client

    monkeypatch.setattr(realtime.httpx, "AsyncClient", make_http_client)
    webhook_resource = FakeWebhookResource({})
    webhook_client = FakeWebhookClient(webhook_resource)
    client = OpenAIRealtimeClient(
        configured_settings(),
        webhook_client=cast(Any, webhook_client),
    )
    session = {"type": "realtime", "model": "gpt-realtime-2.1"}

    await client.accept_call("call_abc/def", session)
    await client.reject_call("call_reject", status_code=486)
    await client.hangup_call("call_hangup")
    await client.probe()
    await client.close()

    assert constructed["base_url"] == "https://api.openai.com/v1"
    assert constructed["headers"] == {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
        "OpenAI-Project": "proj_hotline_test",
        "User-Agent": "agent-hotline/0.2",
    }
    assert http_client.requests == [
        (
            "POST",
            "/realtime/calls/call_abc%2Fdef/accept",
            session,
        ),
        (
            "POST",
            "/realtime/calls/call_reject/reject",
            {"status_code": 486},
        ),
        (
            "POST",
            "/realtime/calls/call_hangup/hangup",
            None,
        ),
        (
            "GET",
            "/models/gpt-realtime-2.1",
            None,
        ),
    ]
    assert http_client.closed
    assert webhook_client.closed


@pytest.mark.asyncio
async def test_client_rejects_unsupported_sip_status_without_making_a_request() -> None:
    http_client = StubHTTPClient()
    webhook_client = FakeWebhookClient(FakeWebhookResource({}))
    client = OpenAIRealtimeClient(
        configured_settings(),
        client=cast(Any, http_client),
        webhook_client=cast(Any, webhook_client),
    )

    with pytest.raises(ValueError, match="unsupported SIP"):
        await client.reject_call("call_reject", status_code=200)

    assert http_client.requests == []


@pytest.mark.asyncio
async def test_client_api_errors_never_echo_response_bodies_or_secrets() -> None:
    response = httpx.Response(
        401,
        headers={"x-request-id": "req_safe_123"},
        json={"error": (f"{OPENAI_API_KEY} {OPENAI_WEBHOOK_SECRET} {OWNER_PIN} {OWNER_PHONE}")},
    )
    http_client = StubHTTPClient([response])
    client = OpenAIRealtimeClient(
        configured_settings(),
        client=cast(Any, http_client),
        webhook_client=cast(
            Any,
            FakeWebhookClient(FakeWebhookResource({})),
        ),
    )

    with pytest.raises(OpenAIRealtimeAPIError, match="HTTP 401") as raised:
        await client.hangup_call("call_error")

    message = str(raised.value)
    assert raised.value.status_code == 401
    assert "req_safe_123" in message
    for secret in (
        OPENAI_API_KEY,
        OPENAI_WEBHOOK_SECRET,
        OWNER_PIN,
        OWNER_PHONE,
    ):
        assert secret not in message


def test_webhook_unwrap_verifies_the_exact_utf8_body_and_model_dump() -> None:
    payload = {
        "id": "evt_verified",
        "type": "realtime.call.incoming",
        "data": {"call_id": "call_verified", "note": "café"},
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    resource = FakeWebhookResource(DumpableWebhookEvent(payload))
    client = OpenAIRealtimeClient(
        configured_settings(),
        client=cast(Any, StubHTTPClient()),
        webhook_client=cast(Any, FakeWebhookClient(resource)),
    )
    headers = {
        "webhook-id": "wh_verified",
        "webhook-signature": "v1,signature",
    }

    assert client.unwrap_webhook(raw, headers) == payload
    assert resource.calls == [(raw.decode("utf-8"), headers)]


@pytest.mark.parametrize(
    ("raw", "resource", "message"),
    [
        (
            b"\xff",
            FakeWebhookResource({}),
            "not UTF-8",
        ),
        (
            b"{}",
            FakeWebhookResource(
                {},
                error=InvalidWebhookSignatureError("unsafe provider detail"),
            ),
            "signature is invalid",
        ),
        (
            b"{}",
            FakeWebhookResource(["not", "an", "object"]),
            "payload is malformed",
        ),
    ],
)
def test_webhook_unwrap_fails_closed_without_echoing_provider_details(
    raw: bytes,
    resource: FakeWebhookResource,
    message: str,
) -> None:
    client = OpenAIRealtimeClient(
        configured_settings(),
        client=cast(Any, StubHTTPClient()),
        webhook_client=cast(Any, FakeWebhookClient(resource)),
    )

    with pytest.raises(OpenAIWebhookVerificationError, match=message) as raised:
        client.unwrap_webhook(raw, {"webhook-signature": "invalid"})

    assert "unsafe provider detail" not in str(raised.value)


@pytest.mark.asyncio
async def test_manager_ignores_verified_non_call_events_without_mutation(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )
    raw = webhook_bytes(
        {
            "id": "evt_unhandled",
            "type": "response.completed",
            "data": {"call_id": "call_unhandled"},
        }
    )

    result = await manager.handle_webhook(raw, verified_headers("wh_unhandled"))

    assert not result.handled
    assert not result.accepted
    assert client.accepted == []
    assert client.rejected == []
    assert await store.list_events() == []
    await manager.close()


@pytest.mark.asyncio
async def test_inbound_allowlisted_call_is_accepted_once_across_webhook_replays(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=sockets,
    )
    payload = await admitted_incoming_webhook(
        store,
        call_id="call_inbound_allowed",
    )
    raw = webhook_bytes(payload)

    first = await manager.handle_webhook(raw, verified_headers("wh_inbound_1"))
    await wait_until(lambda: bool(sockets.connections))
    exact_replay = await manager.handle_webhook(
        raw,
        verified_headers("wh_inbound_1"),
    )
    provider_duplicate = await manager.handle_webhook(
        raw,
        verified_headers("wh_inbound_2"),
    )

    assert first.handled and first.accepted and not first.duplicate
    assert exact_replay.handled and exact_replay.accepted and exact_replay.duplicate
    assert provider_duplicate.accepted and provider_duplicate.duplicate
    assert len(client.accepted) == 1
    accepted_call_id, session = client.accepted[0]
    assert accepted_call_id == "call_inbound_allowed"
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-realtime-2.1"
    assert "you're talking directly to your agent" in session["instructions"]
    assert session["tools"] == realtime_tool_definitions()

    persisted = await store.get_session_by_interaction("call_inbound_allowed")
    assert persisted is not None
    assert persisted.event_id == first.event_id
    assert persisted.direction is ContactDirection.INBOUND_CONTROL
    assert persisted.provider == "openai_realtime"
    assert persisted.attempt_id == TWILIO_CALL_SID
    assert persisted.state is SessionState.CONNECTED
    assert manager.active_calls == 1

    await manager.close()


@pytest.mark.asyncio
async def test_max_active_admission_serializes_concurrent_calls_and_deduplicates_rejection(
    store: SQLiteStore,
) -> None:
    settings = configured_settings(hotline_max_active_calls=1)
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=sockets,
    )
    payloads = {
        "call_capacity_a": await admitted_incoming_webhook(
            store,
            call_id="call_capacity_a",
            event_id="evt_capacity_a",
            carrier_call_sid="CA" + ("a" * 32),
        ),
        "call_capacity_b": await admitted_incoming_webhook(
            store,
            call_id="call_capacity_b",
            event_id="evt_capacity_b",
            carrier_call_sid="CA" + ("c" * 32),
        ),
    }
    deliveries = {
        call_id: (
            webhook_bytes(payload),
            verified_headers(f"wh_{call_id}"),
        )
        for call_id, payload in payloads.items()
    }

    first_results = await asyncio.gather(
        *(manager.handle_webhook(raw, headers) for raw, headers in deliveries.values())
    )
    await wait_until(lambda: manager.active_calls == 1)
    accepted = next(result for result in first_results if result.accepted)
    rejected = next(result for result in first_results if not result.accepted)

    assert accepted.duplicate is False
    assert rejected.duplicate is False
    assert rejected.reason == "the owner already has the maximum number of active calls"
    assert client.accepted[0][0] == accepted.call_id
    assert client.rejected == [(rejected.call_id, 486)]
    assert len(await store.list_events()) == 1

    replay_results = {
        call_id: await manager.handle_webhook(raw, headers)
        for call_id, (raw, headers) in deliveries.items()
    }

    assert all(result.duplicate for result in replay_results.values())
    assert replay_results[accepted.call_id or ""].accepted is True
    assert replay_results[rejected.call_id or ""].accepted is False
    assert len(client.accepted) == 1
    assert client.rejected == [(rejected.call_id, 486)]
    assert manager.active_calls == 1

    await manager.close()


@pytest.mark.asyncio
async def test_begin_inbound_active_session_error_maps_to_sip_busy(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configured_settings()
    coordinator = real_coordinator(settings, store)

    async def reject_busy(*_args: object, **_kwargs: object) -> object:
        raise ActiveSessionError("synthetic owner-channel collision")

    monkeypatch.setattr(coordinator, "begin_inbound_session", reject_busy)
    client = FakeControlClient()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=coordinator,
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )
    raw = webhook_bytes(
        await admitted_incoming_webhook(
            store,
            call_id="call_inbound_store_busy",
            event_id="evt_inbound_store_busy",
        )
    )
    headers = verified_headers("wh_inbound_store_busy")

    result = await manager.handle_webhook(raw, headers)
    replay = await manager.handle_webhook(raw, headers)

    assert result.handled and not result.accepted and not result.duplicate
    assert result.reason == "another owner call is already active"
    assert replay.handled and not replay.accepted and replay.duplicate
    assert client.rejected == [("call_inbound_store_busy", 486)]
    assert client.accepted == []
    assert manager.active_calls == 0

    await manager.close()


@pytest.mark.asyncio
async def test_startup_continuity_gap_terminates_both_call_legs_without_reaccepting(
    store: SQLiteStore,
) -> None:
    settings = configured_settings(hotline_max_active_calls=1)
    event, session = await persist_recoverable_realtime_call(
        store,
        call_id="call_recovered_startup",
    )

    client = FakeControlClient()
    sockets = FakeSocketFactory()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=sockets,
    )

    terminated = await manager.recover_active_calls()
    repeated = await manager.recover_active_calls()

    assert terminated == 1
    assert repeated == 0
    assert client.accepted == []
    assert client.rejected == []
    assert client.hung_up == ["call_recovered_startup"]
    assert provider.terminated_attempts == [session.attempt_id]
    assert sockets.connections == []
    assert manager.active_calls == 0
    assert manager._workers == {}
    assert manager._conversations == {}
    assert (await store.require_event(event.event_id)).state is EventState.FAILED
    persisted = await store.get_session(session.session_id)
    assert persisted is not None
    assert persisted.state is SessionState.FAILED

    await manager.close()


@pytest.mark.asyncio
async def test_startup_recovery_drains_more_than_one_active_session_despite_capacity(
    store: SQLiteStore,
) -> None:
    await store.connection.execute("DROP INDEX ux_sessions_active_owner")
    await store.connection.commit()
    persisted = [
        await persist_recoverable_realtime_call(
            store,
            call_id=f"call_recovery_capacity_{index}",
            attempt_id=f"CA{index:032x}",
        )
        for index in range(3)
    ]
    settings = configured_settings(hotline_max_active_calls=1)
    client = FakeControlClient()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    terminated = await manager.recover_active_calls()
    replay = await manager.recover_active_calls()

    assert terminated == 3
    assert replay == 0
    assert set(client.hung_up) == {f"call_recovery_capacity_{index}" for index in range(3)}
    assert set(provider.terminated_attempts) == {f"CA{index:032x}" for index in range(3)}
    for event, session in persisted:
        assert (await store.require_event(event.event_id)).state is EventState.FAILED
        recovered_session = await store.get_session(session.session_id)
        assert recovered_session is not None
        assert recovered_session.state is SessionState.FAILED

    await manager.close()


@pytest.mark.asyncio
async def test_startup_recovery_drains_orphans_beyond_one_reconciliation_batch(
    store: SQLiteStore,
) -> None:
    orphan_count = 23
    parent_ids: list[str] = []
    provider_ids: list[str] = []
    for index in range(orphan_count):
        call_sid = f"CA{index + 100:032x}"
        provider_call_id = f"call_orphan_recovery_{index}"
        admission = await store.issue_carrier_admission(
            call_sid,
            caller_phone=OWNER_PHONE,
            ttl_seconds=300,
        )
        await store.consume_carrier_admission(
            call_sid,
            caller_phone=admission.caller_phone,
            admission_nonce=admission.admission_nonce,
            expires_at_epoch=int(admission.expires_at.timestamp()),
            provider_call_id=provider_call_id,
        )
        parent_ids.append(call_sid)
        provider_ids.append(provider_call_id)

    settings = configured_settings()
    client = FakeControlClient()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    terminated_sessions = await manager.recover_active_calls()
    replay = await manager.recover_active_calls()

    assert terminated_sessions == 0
    assert replay == 0
    assert set(client.hung_up) == set(provider_ids)
    assert set(provider.terminated_attempts) == set(parent_ids)
    jobs = await store.list_call_termination_jobs(limit=100)
    assert len(jobs) == orphan_count * 2
    assert all(job.state.value == "confirmed" for job in jobs)
    assert await store.list_orphaned_consumed_carrier_legs(limit=100) == []

    await manager.close()


@pytest.mark.asyncio
async def test_processed_webhook_continuity_gap_terminates_both_call_legs(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    event, _session = await persist_recoverable_realtime_call(
        store,
        call_id="call_recovered_from_receipt",
    )
    payload = incoming_webhook(
        call_id="call_recovered_from_receipt",
        event_id="evt_recovered_from_receipt",
    )
    raw = webhook_bytes(payload)
    webhook_id = "wh_recovered_from_receipt"
    receipt_key = f"openai:{webhook_id}"
    claim = await store.claim_ingress_receipt(
        receipt_key,
        kind="openai.realtime.call.incoming",
        subject_id="call_recovered_from_receipt",
        fingerprint=hashlib.sha256(raw).hexdigest(),
    )
    assert claim.created and not claim.processed
    await store.complete_ingress_receipt(receipt_key)

    client = FakeControlClient()
    sockets = FakeSocketFactory()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=sockets,
    )

    result = await manager.handle_webhook(raw, verified_headers(webhook_id))

    assert result.handled and not result.accepted and result.duplicate
    assert result.call_id == "call_recovered_from_receipt"
    assert result.event_id is None
    assert client.accepted == []
    assert client.rejected == []
    assert client.hung_up == ["call_recovered_from_receipt"]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert sockets.connections == []
    assert manager.active_calls == 0
    assert (await store.require_event(event.event_id)).state is EventState.FAILED

    await manager.close()


@pytest.mark.asyncio
async def test_inbound_unallowlisted_call_is_rejected_once_and_discloses_nothing(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )
    payload = await admitted_incoming_webhook(
        store,
        call_id="call_inbound_denied",
        caller="+12025550999",
    )
    raw = webhook_bytes(payload)
    headers = verified_headers("wh_inbound_denied")

    first = await manager.handle_webhook(raw, headers)
    duplicate = await manager.handle_webhook(raw, headers)

    assert first.handled and not first.accepted and not first.duplicate
    assert first.reason is not None
    assert "not allowlisted" in first.reason
    assert "+12025550999" not in first.reason
    assert duplicate.handled and not duplicate.accepted and duplicate.duplicate
    assert client.rejected == [("call_inbound_denied", 403)]
    assert client.accepted == []
    assert await store.list_events() == []
    assert manager.active_calls == 0
    await manager.close()


@pytest.mark.asyncio
async def test_direct_sip_with_spoofable_from_header_is_rejected(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )
    payload = incoming_webhook(
        call_id="call_direct_sip_spoof",
        caller=OWNER_PHONE,
        carrier_correlated=False,
    )

    result = await manager.handle_webhook(
        webhook_bytes(payload),
        verified_headers("wh_direct_sip_spoof"),
    )

    assert not result.accepted
    assert result.reason == "verified carrier correlation is required"
    assert client.rejected == [("call_direct_sip_spoof", 403)]
    assert await store.list_events() == []
    await manager.close()


@pytest.mark.asyncio
async def test_verified_webhook_id_conflict_fails_closed(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )
    first = await admitted_incoming_webhook(
        store,
        call_id="call_conflict_one",
        caller="+12025550998",
    )
    changed = incoming_webhook(
        call_id="call_conflict_two",
        caller="+12025550998",
    )
    headers = verified_headers("wh_conflicting_reuse")

    await manager.handle_webhook(webhook_bytes(first), headers)
    with pytest.raises(ConflictError, match="reused with different content"):
        await manager.handle_webhook(webhook_bytes(changed), headers)

    assert client.rejected == [("call_conflict_one", 403)]
    assert client.accepted == []
    await manager.close()


@pytest.mark.asyncio
async def test_outbound_signed_parent_correlates_distinct_twilio_child_leg(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    event = EscalationEvent(
        kind="approval",
        summary="Production deployment is ready.",
        question="Approve the exact production deployment?",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = ContactSession(
        event_id=event.event_id,
        direction=ContactDirection.OUTBOUND_ESCALATION,
        state=SessionState.DIALING,
        attempt_id=TWILIO_CALL_SID,
        provider="twilio",
    )
    await store.create_session(session)

    signature = build_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=TWILIO_CALL_SID,
        event_id=event.event_id,
    )
    payload = incoming_webhook(
        call_id="call_outbound_openai",
        event_id="evt_openai_outbound_webhook",
        extra_headers={
            "X-Hotline-Direction": "outbound",
            "X-Hotline-Call-Sid": TWILIO_CALL_SID,
            "X-Hotline-Event-Id": event.event_id,
            "X-Hotline-Signature": signature,
        },
    )
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=sockets,
    )

    result = await manager.handle_webhook(
        webhook_bytes(payload),
        verified_headers("wh_outbound"),
    )
    await wait_until(lambda: bool(sockets.connections))

    assert result.accepted and result.event_id == event.event_id
    assert len(client.accepted) == 1
    assert "it's your agent" in client.accepted[0][1]["instructions"]
    linked = await store.get_session(session.session_id)
    assert linked is not None
    assert linked.attempt_id == TWILIO_CALL_SID
    assert linked.attempt_id != TWILIO_CHILD_CALL_SID
    assert linked.interaction_id == "call_outbound_openai"
    assert linked.state is SessionState.CONNECTED
    current_event = await store.require_event(event.event_id)
    assert current_event.state is EventState.AWAITING_DECISION

    await manager.close()


@pytest.mark.asyncio
async def test_outbound_parent_binding_is_idempotent_and_stops_terminal_event(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    event = EscalationEvent(
        kind="approval",
        summary="The outbound parent must bind before TwiML is returned.",
        question="Should this call continue?",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
            provider="openai_realtime",
        )
    )
    client = FakeControlClient()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    first, first_should_dial = await manager.bind_outbound_carrier_parent(
        event_id=event.event_id,
        call_sid=TWILIO_CALL_SID,
    )
    replay, replay_should_dial = await manager.bind_outbound_carrier_parent(
        event_id=event.event_id,
        call_sid=TWILIO_CALL_SID,
    )

    assert first.session_id == session.session_id
    assert replay.session_id == session.session_id
    assert first.attempt_id == TWILIO_CALL_SID
    assert replay.attempt_id == TWILIO_CALL_SID
    assert first_should_dial is True
    assert replay_should_dial is True
    assert provider.terminated_attempts == []

    await store.transition_event(event.event_id, EventState.EXPIRED)
    terminal, terminal_should_dial = await manager.bind_outbound_carrier_parent(
        event_id=event.event_id,
        call_sid=TWILIO_CALL_SID,
    )

    assert terminal_should_dial is False
    assert terminal.state is SessionState.CANCELLED
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert client.hung_up == []
    jobs = await store.list_call_termination_jobs(session_id=session.session_id)
    assert len(jobs) == 1
    assert jobs[0].leg.value == "carrier"
    assert jobs[0].target_id == TWILIO_CALL_SID
    assert jobs[0].state.value == "confirmed"
    assert (await store.require_event(event.event_id)).state is EventState.EXPIRED

    await manager.close()


@pytest.mark.asyncio
async def test_terminal_ghost_callback_retries_durable_call_termination_after_restart(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingControlClient(FakeControlClient):
        async def hangup_call(self, call_id: str) -> None:
            self.hung_up.append(call_id)
            raise RuntimeError("synthetic OpenAI termination failure")

    class FailingCallProvider(FakeCallProvider):
        async def terminate_call(self, attempt_id: str) -> None:
            self.terminated_attempts.append(attempt_id)
            raise RuntimeError("synthetic carrier termination failure")

    settings = configured_settings()
    event = EscalationEvent(
        kind="approval",
        summary="A terminal event received its parent CallSid after cancellation.",
        question="This event is already terminal.",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
            interaction_id="call_terminal_ghost",
            provider="openai_realtime",
        )
    )
    await store.transition_event(event.event_id, EventState.EXPIRED)
    failing_client = FailingControlClient()
    failing_provider = FailingCallProvider()
    first_manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(
            settings,
            store,
            provider=failing_provider,
        ),
        client=cast(Any, failing_client),
        socket_factory=FakeSocketFactory(),
    )

    result = await first_manager.handle_carrier_status(
        call_sid=TWILIO_CALL_SID,
        status="completed",
        receipt_id="receipt_terminal_ghost",
        correlated_event_id=event.event_id,
    )

    assert result == {
        "accepted": True,
        "terminal": True,
        "event_id": event.event_id,
        "termination": "scheduled",
    }
    linked = await store.get_session(session.session_id)
    assert linked is not None
    assert linked.attempt_id == TWILIO_CALL_SID
    assert linked.state is SessionState.CANCELLED
    pending = await store.list_call_termination_jobs(session_id=session.session_id)
    assert {job.leg.value for job in pending} == {"openai", "carrier"}
    assert all(job.state.value == "pending" for job in pending)
    assert all(job.attempts == 1 for job in pending)
    assert failing_client.hung_up == ["call_terminal_ghost"]
    assert failing_provider.terminated_attempts == [TWILIO_CALL_SID]
    await first_manager.close()

    monkeypatch.setattr(
        storage_module,
        "utc_now",
        lambda: utc_now() + timedelta(minutes=5),
    )
    recovery_client = FakeControlClient()
    recovery_provider = FakeCallProvider()
    recovered_manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(
            settings,
            store,
            provider=recovery_provider,
        ),
        client=cast(Any, recovery_client),
        socket_factory=FakeSocketFactory(),
    )

    retried = await recovered_manager.reconcile_pending_call_terminations()

    assert retried == 2
    confirmed = await store.list_call_termination_jobs(session_id=session.session_id)
    assert all(job.state.value == "confirmed" for job in confirmed)
    assert all(job.attempts == 2 for job in confirmed)
    assert recovery_client.hung_up == ["call_terminal_ghost"]
    assert recovery_provider.terminated_attempts == [TWILIO_CALL_SID]
    assert (await store.require_event(event.event_id)).state is EventState.EXPIRED

    await recovered_manager.close()


@pytest.mark.asyncio
async def test_max_age_expires_no_deadline_notification_with_only_carrier_leg(
    store: SQLiteStore,
) -> None:
    settings = configured_settings(hotline_max_call_duration_seconds=60)
    event = EscalationEvent(
        kind="status",
        summary="A nonblocking notification call exceeded its maximum lifetime.",
        blocking=False,
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
            attempt_id=TWILIO_CALL_SID,
            provider="openai_realtime",
            started_at=utc_now() - timedelta(seconds=61),
        )
    )
    client = FakeControlClient()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    first = await manager.expire_overdue_calls()
    replay = await manager.expire_overdue_calls()

    assert first == 1
    assert replay == 0
    assert event.deadline_at is None
    assert (await store.require_event(event.event_id)).state is EventState.FAILED
    persisted = await store.get_session(session.session_id)
    assert persisted is not None
    assert persisted.state is SessionState.FAILED
    assert persisted.failure_reason == "maximum call duration elapsed"
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert client.hung_up == []
    jobs = await store.list_call_termination_jobs(session_id=session.session_id)
    assert len(jobs) == 1
    assert jobs[0].leg.value == "carrier"
    assert jobs[0].state.value == "confirmed"

    await manager.close()


@pytest.mark.asyncio
async def test_termination_enqueue_failure_attempts_both_legs_and_keeps_state_active(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configured_settings()
    event = EscalationEvent(
        kind="status",
        summary="Durable termination enqueue will fail synthetically.",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
            attempt_id=TWILIO_CALL_SID,
            interaction_id="call_enqueue_failure",
            provider="openai_realtime",
        )
    )
    client = FakeControlClient()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    async def fail_enqueue(**_kwargs: object) -> list[object]:
        raise StorageError("synthetic durable queue failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "ensure_call_termination_jobs", fail_enqueue)
        with pytest.raises(
            OpenAIRealtimeError,
            match="could not be durably enqueued",
        ):
            await manager.schedule_call_termination(session)

    assert client.hung_up == ["call_enqueue_failure"]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert await store.list_call_termination_jobs(session_id=session.session_id) == []
    assert (await store.require_event(event.event_id)).state is EventState.DIALING
    persisted = await store.get_session(session.session_id)
    assert persisted is not None
    assert persisted.state is SessionState.DIALING

    await manager.close()


@pytest.mark.asyncio
async def test_graceful_close_drains_dialing_parent_only_session(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    event = EscalationEvent(
        kind="status",
        summary="Shutdown began before the OpenAI SIP child arrived.",
        blocking=False,
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
            attempt_id=TWILIO_CALL_SID,
            provider="openai_realtime",
        )
    )
    client = FakeControlClient()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    await manager.close()
    await manager.close()

    assert client.hung_up == []
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert (await store.require_event(event.event_id)).state is EventState.FAILED
    persisted = await store.get_session(session.session_id)
    assert persisted is not None
    assert persisted.state is SessionState.FAILED
    jobs = await store.list_call_termination_jobs(session_id=session.session_id)
    assert len(jobs) == 1
    assert jobs[0].leg.value == "carrier"
    assert jobs[0].state.value == "confirmed"


@pytest.mark.parametrize("failure", ["bad_signature", "wrong_surrogate"])
@pytest.mark.asyncio
async def test_outbound_correlation_tampering_is_rejected_before_acceptance(
    store: SQLiteStore,
    failure: str,
) -> None:
    settings = configured_settings()
    event = EscalationEvent(
        kind="approval",
        summary="Deployment is ready.",
        question="Approve deployment?",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.DIALING,
            attempt_id=TWILIO_CALL_SID,
            provider="twilio",
        )
    )
    surrogate = (
        "outbound:another_event" if failure == "wrong_surrogate" else f"outbound:{event.event_id}"
    )
    signature = build_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=surrogate,
        event_id=event.event_id,
    )
    if failure == "bad_signature":
        signature = "forged-signature"
    payload = incoming_webhook(
        call_id=f"call_tampered_{failure}",
        extra_headers={
            "X-Hotline-Direction": "outbound",
            "X-Hotline-Call-Sid": surrogate,
            "X-Hotline-Event-Id": event.event_id,
            "X-Hotline-Signature": signature,
        },
    )
    client = FakeControlClient()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store),
        client=cast(Any, client),
        socket_factory=FakeSocketFactory(),
    )

    result = await manager.handle_webhook(
        webhook_bytes(payload),
        verified_headers(f"wh_tampered_{failure}"),
    )

    assert result.handled and not result.accepted
    assert client.accepted == []
    assert client.rejected == [(f"call_tampered_{failure}", 403)]
    await manager.close()


@pytest.mark.asyncio
async def test_sideband_connects_with_call_auth_and_requests_one_greeting(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id="call/sideband",
    )

    task = asyncio.create_task(conversation.run())
    await wait_until(lambda: bool(sockets.socket.sent))

    assert sockets.connections == [
        (
            "wss://api.openai.com/v1/realtime?call_id=call%2Fsideband",
            {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Project": "proj_hotline_test",
                "User-Agent": "agent-hotline/0.2",
            },
        )
    ]
    assert len(sockets.socket.sent) == 1
    greeting = sockets.socket.sent[0]
    assert greeting["type"] == "response.create"
    assert greeting["event_id"].startswith("evt_hotline_")
    assert greeting["response"] == {
        "instructions": "Begin the call now using the configured opening and conversation flow."
    }
    assert OPENAI_API_KEY not in "".join(sockets.socket.sent_raw)

    await conversation.stop()
    with pytest.raises(SocketEnded):
        await task


@pytest.mark.asyncio
async def test_uncorrelated_realtime_error_fails_closed_on_both_call_legs(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    provider = FakeCallProvider()
    sockets = FakeSocketFactory()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=sockets,
    )
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id="call_uncorrelated_error",
    )

    worker = asyncio.create_task(manager._run_conversation(conversation))
    await wait_until(lambda: len(sockets.socket.sent) == 1)
    sockets.socket.push(
        {
            "type": "error",
            "error": {"type": "server_error", "message": "provider detail"},
        }
    )
    await asyncio.wait_for(worker, timeout=2)

    assert client.hung_up == ["call_uncorrelated_error"]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    await manager.close()


@pytest.mark.parametrize("response_status", ["failed", "incomplete"])
@pytest.mark.asyncio
async def test_non_successful_response_terminal_fails_closed_on_both_call_legs(
    store: SQLiteStore,
    response_status: str,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    provider = FakeCallProvider()
    sockets = FakeSocketFactory()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=sockets,
    )
    call_id = f"call_response_{response_status}"
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id=call_id,
    )

    worker = asyncio.create_task(manager._run_conversation(conversation))
    await wait_until(lambda: len(sockets.socket.sent) == 1)
    sockets.socket.push(
        {
            "type": "response.done",
            "response": {
                "id": f"resp_{response_status}",
                "status": response_status,
                "status_details": {"reason": "test_failure"},
                "output": [],
            },
        }
    )
    await asyncio.wait_for(worker, timeout=2)

    assert client.hung_up == [call_id]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    await manager.close()


@pytest.mark.asyncio
async def test_normal_sideband_close_reconciles_as_completed(
    store: SQLiteStore,
) -> None:
    call_id = "call_normal_peer_close"
    event, session = await persist_recoverable_realtime_call(store, call_id=call_id)
    settings = configured_settings()
    client = FakeControlClient()
    provider = FakeCallProvider()
    socket = FakeRealtimeSocket()
    socket.incoming.put_nowait(
        ConnectionClosedOK(Close(1000, "caller hung up"), None)
    )
    sockets = FakeSocketFactory(socket)
    coordinator = real_coordinator(settings, store, provider=provider)
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=coordinator,
        client=cast(Any, client),
        socket_factory=sockets,
    )
    conversation = RealtimeConversation(
        settings=settings,
        store=store,
        client=cast(Any, client),
        socket_factory=sockets,
        call_id=call_id,
        attempt_id=TWILIO_CALL_SID,
        event_id=event.event_id,
        session_id=session.session_id,
        direction="inbound_control",
        coordinator=coordinator,
    )

    await manager._run_conversation(conversation)

    persisted = await store.get_session(session.session_id)
    assert persisted is not None
    assert persisted.state is SessionState.COMPLETED
    assert persisted.failure_reason is None
    assert client.hung_up == [call_id]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    await manager.close()


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.function_call_arguments.done",
            "call_id": "call_tool_123",
            "name": "get_hotline_context",
            "arguments": "{}",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_tool_123",
                "name": "get_hotline_context",
                "arguments": "{}",
            },
        },
    ],
)
def test_per_item_function_call_events_are_not_authoritative(
    event: dict[str, Any],
) -> None:
    assert realtime._extract_function_calls(event) == []


def test_completed_response_extracts_its_function_call() -> None:
    event = {
        "type": "response.done",
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_tool_123",
                    "name": "get_hotline_context",
                    "arguments": "{}",
                }
            ],
        },
    }

    assert realtime._extract_function_calls(event) == [
        ("call_tool_123", "get_hotline_context", "{}")
    ]


def test_cancelled_response_done_does_not_dispatch_embedded_function_call() -> None:
    event = {
        "type": "response.done",
        "response": {
            "status": "cancelled",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_cancelled_tool",
                    "name": "execute_action",
                    "arguments": '{"action_id":"act_cancelled"}',
                }
            ],
        },
    }

    assert realtime._extract_function_calls(event) == []


@pytest.mark.asyncio
async def test_tool_output_precedes_response_and_exact_replay_uses_durable_receipt(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    dispatcher = StaticDispatcher(ToolDispatchResult(payload={"ok": True, "summary": "ready"}))
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = cast(Any, dispatcher)

    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_tool_receipt",
        name="get_hotline_context",
        arguments_json='{"detail":"brief"}',
    )
    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_tool_receipt",
        name="get_hotline_context",
        arguments_json='{"detail":"brief"}',
    )

    assert dispatcher.calls == [
        ("get_hotline_context", {"detail": "brief"}),
    ]
    assert [message["type"] for message in sockets.socket.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    first_output = sockets.socket.sent[0]["item"]
    assert first_output["type"] == "function_call_output"
    assert first_output["call_id"] == "call_tool_receipt"
    assert json.loads(first_output["output"]) == {
        "ok": True,
        "summary": "ready",
    }
    assert sockets.socket.sent[1]["response"]["metadata"] == {
        "hotline_tool_call_id": "call_tool_receipt"
    }
    receipt = await store.get_realtime_tool_delivery(
        "call_realtime_test",
        "call_tool_receipt",
    )
    assert receipt is not None
    assert receipt.delivery_state == "delivered"


@pytest.mark.asyncio
async def test_tool_delivery_advances_only_after_both_correlated_server_acks(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    socket = FakeRealtimeSocket(auto_ack_delivery=False)
    sockets = FakeSocketFactory(socket)
    dispatcher = StaticDispatcher(
        ToolDispatchResult(payload={"ok": True, "summary": "acknowledged"})
    )
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = cast(Any, dispatcher)
    conversation._socket = socket
    read_task = asyncio.create_task(conversation._read_loop(socket))
    tool_task = asyncio.create_task(
        conversation._process_tool_call(
            socket,
            tool_call_id="call_ack_phases",
            name="get_hotline_context",
            arguments_json="{}",
        )
    )
    try:
        await wait_until(lambda: len(socket.sent) == 1)
        output_event = socket.sent[0]
        item_id = output_event["item"]["id"]
        pending = await store.get_realtime_tool_delivery(
            conversation.call_id,
            "call_ack_phases",
        )
        assert pending is not None
        assert pending.delivery_state == "pending"
        assert not tool_task.done()

        socket.push(
            {
                "type": "conversation.item.created",
                "item": {"id": "item_for_another_tool"},
            }
        )
        await asyncio.sleep(0)
        assert not tool_task.done()
        socket.push(
            {
                "type": "conversation.item.created",
                "item": {"id": item_id},
            }
        )

        await wait_until(lambda: len(socket.sent) == 2)
        response_event = socket.sent[1]
        output_sent = await store.get_realtime_tool_delivery(
            conversation.call_id,
            "call_ack_phases",
        )
        assert output_sent is not None
        assert output_sent.delivery_state == "output_sent"
        assert response_event["response"]["metadata"] == {"hotline_tool_call_id": "call_ack_phases"}
        assert not tool_task.done()

        socket.push(
            {
                "type": "response.created",
                "response": {"metadata": {"hotline_tool_call_id": "call_for_another_tool"}},
            }
        )
        await asyncio.sleep(0)
        assert not tool_task.done()
        socket.push(
            {
                "type": "response.created",
                "response": {"metadata": {"hotline_tool_call_id": "call_ack_phases"}},
            }
        )
        await tool_task

        delivered = await store.get_realtime_tool_delivery(
            conversation.call_id,
            "call_ack_phases",
        )
        assert delivered is not None
        assert delivered.delivery_state == "delivered"
        assert dispatcher.calls == [("get_hotline_context", {})]
    finally:
        if not tool_task.done():
            tool_task.cancel()
            await asyncio.gather(tool_task, return_exceptions=True)
        await socket.close()
        await asyncio.gather(read_task, return_exceptions=True)
        await conversation.stop()


@pytest.mark.asyncio
async def test_correlated_tool_delivery_rejection_fails_closed_on_both_call_legs(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    provider = FakeCallProvider()
    socket = FakeRealtimeSocket(auto_ack_delivery=False)
    sockets = FakeSocketFactory(socket)
    coordinator = real_coordinator(settings, store, provider=provider)
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=coordinator,
        client=cast(Any, client),
        socket_factory=sockets,
    )
    dispatcher = StaticDispatcher()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id="call_fatal_tool_reject",
    )
    conversation.dispatcher = cast(Any, dispatcher)
    worker = asyncio.create_task(manager._run_conversation(conversation))

    await wait_until(lambda: len(socket.sent) == 1)
    socket.push(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_fatal_reject_tool",
                        "name": "get_hotline_context",
                        "arguments": "{}",
                    }
                ],
            },
        }
    )
    await wait_until(lambda: len(socket.sent) == 2)
    rejected_event_id = socket.sent[1]["event_id"]
    socket.push(
        {
            "type": "error",
            "error": {
                "event_id": rejected_event_id,
                "type": "invalid_request_error",
            },
        }
    )
    await asyncio.wait_for(worker, timeout=2)

    assert dispatcher.calls == [("get_hotline_context", {})]
    assert client.hung_up == ["call_fatal_tool_reject"]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    receipt = await store.get_realtime_tool_delivery(
        "call_fatal_tool_reject",
        "call_fatal_reject_tool",
    )
    assert receipt is not None
    assert receipt.delivery_state == "pending"
    assert socket.close_reason == "tool delivery failed"
    await manager.close()


@pytest.mark.asyncio
async def test_pending_tool_delivery_at_connection_start_fails_closed_without_replay(
    store: SQLiteStore,
) -> None:
    call_id = "call_pending_delivery_gap"
    arguments_hash = hashlib.sha256(b"{}").hexdigest()
    await store.record_realtime_tool_receipt(
        call_id,
        "call_pending_from_prior_socket",
        tool_name="get_hotline_context",
        arguments_hash=arguments_hash,
        output_json='{"ok":true}',
        request_response=True,
    )
    settings = configured_settings()
    client = FakeControlClient()
    provider = FakeCallProvider()
    sockets = FakeSocketFactory()
    coordinator = real_coordinator(settings, store, provider=provider)
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=coordinator,
        client=cast(Any, client),
        socket_factory=sockets,
    )
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id=call_id,
    )

    await manager._run_conversation(conversation)

    assert len(sockets.connections) == 1
    assert sockets.socket.sent == []
    assert client.hung_up == [call_id]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    receipt = await store.get_realtime_tool_delivery(
        call_id,
        "call_pending_from_prior_socket",
    )
    assert receipt is not None
    assert receipt.delivery_state == "pending"
    await manager.close()


@pytest.mark.asyncio
async def test_tool_call_id_reuse_with_different_arguments_fails_closed(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    sockets = FakeSocketFactory()
    dispatcher = StaticDispatcher()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = cast(Any, dispatcher)

    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_tool_conflict",
        name="inspect_agent_task",
        arguments_json='{"reference":"thread-one"}',
    )
    await conversation._process_tool_call(
        sockets.socket,
        tool_call_id="call_tool_conflict",
        name="inspect_agent_task",
        arguments_json='{"reference":"thread-two"}',
    )

    assert len(dispatcher.calls) == 1
    conflict = json.loads(sockets.socket.sent[-2]["item"]["output"])
    assert conflict == {
        "ok": False,
        "error": "The tool call identifier was reused with different content.",
        "retryable": False,
    }
    assert sockets.socket.sent[-1]["type"] == "response.create"
    assert sockets.socket.sent[-1]["event_id"].startswith("evt_hotline_")


@pytest.mark.parametrize(
    "arguments_json",
    [
        "{",
        "[]",
        '{"reference":"one","reference":"two"}',
        json.dumps({"value": "x" * (16 * 1024)}),
    ],
)
@pytest.mark.asyncio
async def test_malformed_duplicate_or_oversized_tool_arguments_never_dispatch(
    store: SQLiteStore,
    arguments_json: str,
) -> None:
    settings = configured_settings()
    sockets = FakeSocketFactory()
    dispatcher = StaticDispatcher()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = cast(Any, dispatcher)

    await conversation._process_tool_call(
        sockets.socket,
        tool_call_id="call_malformed_args",
        name="inspect_agent_task",
        arguments_json=arguments_json,
    )

    assert dispatcher.calls == []
    assert [event["type"] for event in sockets.socket.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    output = json.loads(sockets.socket.sent[0]["item"]["output"])
    assert output == {
        "ok": False,
        "error": "Tool arguments were malformed.",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_wait_for_user_emits_function_output_without_requesting_speech(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    sockets = FakeSocketFactory()
    dispatcher = RealtimeToolDispatcher(
        settings=settings,
        coordinator=cast(Any, RecordingToolCoordinator()),
        event_id="evt_wait",
        session_id="ses_wait",
        direction="inbound_control",
    )
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = dispatcher

    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_wait_for_user",
        name="wait_for_user",
        arguments_json="{}",
    )
    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_wait_for_user",
        name="wait_for_user",
        arguments_json="{}",
    )

    assert [event["type"] for event in sockets.socket.sent] == [
        "conversation.item.create",
    ]
    assert all(
        json.loads(event["item"]["output"]) == {"ok": True, "waiting": True}
        for event in sockets.socket.sent
    )


@pytest.mark.asyncio
async def test_finish_session_waits_for_goodbye_audio_to_drain_then_hangs_up(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=sockets,
    )
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id="call_finish",
    )
    task = asyncio.create_task(manager._run_conversation(conversation))
    await wait_until(lambda: len(sockets.socket.sent) == 1)

    sockets.socket.push(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_finish_tool",
                        "name": "finish_session",
                        "arguments": "{}",
                    }
                ],
            },
        }
    )
    await wait_until(lambda: len(sockets.socket.sent) == 3)
    assert [event["type"] for event in sockets.socket.sent] == [
        "response.create",
        "conversation.item.create",
        "response.create",
    ]
    assert json.loads(sockets.socket.sent[1]["item"]["output"])["ok"] is True
    assert client.hung_up == []

    sockets.socket.push(
        {
            "type": "response.done",
            "response": {
                "id": "resp_finish_goodbye",
                "status": "completed",
                "output": [],
            },
        }
    )
    await wait_until(lambda: conversation._finish_response_id == "resp_finish_goodbye")
    assert not task.done()
    assert client.hung_up == []

    sockets.socket.push(
        {
            "type": "output_audio_buffer.stopped",
            "response_id": "resp_finish_goodbye",
        }
    )
    await task

    assert client.hung_up == ["call_finish"]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert sockets.socket.closed
    assert sockets.socket.close_reason == "session finished"
    await manager.close()


@pytest.mark.asyncio
async def test_replayed_finish_receipt_still_arms_hangup_after_goodbye(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=client,
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
        call_id="call_finish_replay",
    )
    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_finish_replayed_tool",
        name="finish_session",
        arguments_json="{}",
    )
    conversation._finish_after_response = False
    sockets.socket.sent.clear()
    sockets.socket.sent_raw.clear()

    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_finish_replayed_tool",
        name="finish_session",
        arguments_json="{}",
    )

    assert conversation._finish_after_response is True
    assert sockets.socket.sent == []


@pytest.mark.asyncio
async def test_duplicate_function_terminal_events_dispatch_only_once(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    sockets = FakeSocketFactory()
    dispatcher = StaticDispatcher()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = cast(Any, dispatcher)
    conversation._socket = sockets.socket
    read_task = asyncio.create_task(conversation._read_loop(sockets.socket))
    call_item = {
        "type": "function_call",
        "call_id": "call_terminal_duplicate",
        "name": "get_hotline_context",
        "arguments": "{}",
    }

    sockets.socket.push(
        {
            "type": "response.function_call_arguments.done",
            "call_id": call_item["call_id"],
            "name": call_item["name"],
            "arguments": call_item["arguments"],
        }
    )
    sockets.socket.push(
        {
            "type": "response.output_item.done",
            "item": call_item,
        }
    )
    sockets.socket.push(
        {
            "type": "response.done",
            "response": {"status": "completed", "output": [call_item]},
        }
    )
    await wait_until(lambda: len(sockets.socket.sent) == 2)
    await wait_until(lambda: not conversation._inflight_tool_calls)
    sockets.socket.push(
        {
            "type": "response.done",
            "response": {"status": "completed", "output": [call_item]},
        }
    )
    await wait_until(lambda: not conversation._inflight_tool_calls)

    assert dispatcher.calls == [("get_hotline_context", {})]
    assert len(sockets.socket.sent) == 2
    receipt = await store.get_realtime_tool_receipt(
        conversation.call_id,
        "call_terminal_duplicate",
    )
    assert receipt is not None

    await sockets.socket.close()
    with pytest.raises(SocketEnded):
        await read_task
    await conversation.stop()


def make_dispatcher(
    settings: Settings,
    coordinator: RecordingToolCoordinator | None = None,
) -> tuple[RealtimeToolDispatcher, RecordingToolCoordinator]:
    recording = coordinator or RecordingToolCoordinator()
    return (
        RealtimeToolDispatcher(
            settings=settings,
            coordinator=cast(Any, recording),
            event_id="evt_dispatch",
            session_id="ses_dispatch",
            direction="outbound_escalation",
        ),
        recording,
    )


def enter_keypad(
    dispatcher: RealtimeToolDispatcher,
    value: str,
) -> list[dict[str, Any]]:
    statuses: list[dict[str, Any]] = []
    for key in value:
        status = dispatcher.receive_dtmf(key)
        if status is not None:
            statuses.append(status)
    return statuses


async def prepare_test_decision(
    dispatcher: RealtimeToolDispatcher,
    *,
    instruction: str = "Deploy the reviewed build.",
) -> dict[str, Any]:
    result = await dispatcher.dispatch(
        "prepare_decision",
        {
            "outcome": "approve",
            "instruction": instruction,
            "constraints": ["production only"],
            "approved_action_ids": [],
        },
    )
    return result.payload


async def deliver_readback_reply_and_arm(
    dispatcher: RealtimeToolDispatcher,
    prepared: Mapping[str, Any],
    *,
    response_id: str = "resp_verified_readback",
) -> None:
    readback = prepared.get("response_text") or prepared.get("exact_readback")
    assert isinstance(readback, str)
    dispatcher.note_readback_transcript(response_id, readback)
    dispatcher.note_response_done(response_id)
    dispatcher.note_output_audio_stopped(response_id)
    dispatcher.note_owner_speech_turn()
    armed = await dispatcher.dispatch("arm_owner_verification", {})
    assert armed.payload["armed"] is True


@pytest.mark.asyncio
async def test_arm_is_strict_and_rejected_before_matching_readback_completion() -> None:
    dispatcher, _ = make_dispatcher(configured_settings())
    prepared = await prepare_test_decision(dispatcher)
    readback = prepared["response_text"]

    with pytest.raises(ValueError, match="accepts no arguments"):
        await dispatcher.dispatch(
            "arm_owner_verification",
            {
                "event_id": "evt_attacker_selected",
                "confirmation_pin": OWNER_PIN,
            },
        )
    with pytest.raises(PermissionError, match="readback has not been delivered"):
        await dispatcher.dispatch("arm_owner_verification", {})

    dispatcher.note_readback_transcript("resp_pending", readback)
    dispatcher.note_response_done("resp_pending")
    with pytest.raises(PermissionError, match="readback has not been delivered"):
        await dispatcher.dispatch("arm_owner_verification", {})

    verification = await dispatcher.dispatch("check_owner_verification", {})
    assert verification.payload == {
        "ok": True,
        "active": False,
        "prepared": True,
        "readback_delivered": False,
        "owner_replied": False,
        "verified": False,
        "locked": False,
        "scope": "decision",
        "subject_id": prepared["confirmation_id"],
    }
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#") == []


@pytest.mark.asyncio
async def test_speech_started_before_readback_audio_drains_invalidates_verification() -> None:
    dispatcher, _ = make_dispatcher(configured_settings())
    prepared = await prepare_test_decision(dispatcher)
    readback = prepared["response_text"]

    dispatcher.note_readback_transcript("resp_interrupted", readback)
    dispatcher.note_response_done("resp_interrupted")
    dispatcher.note_owner_speech_started()
    dispatcher.note_output_audio_stopped("resp_interrupted")

    with pytest.raises(PermissionError, match="no longer armable"):
        await dispatcher.dispatch("arm_owner_verification", {})
    status = await dispatcher.dispatch("check_owner_verification", {})
    assert status.payload["prepared"] is True
    assert status.payload["readback_delivered"] is False
    assert status.payload["owner_replied"] is False
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#") == []


@pytest.mark.parametrize("mismatch", ["response_id", "transcript"])
@pytest.mark.asyncio
async def test_wrong_response_id_or_transcript_never_unlocks_arming(
    mismatch: str,
) -> None:
    dispatcher, _ = make_dispatcher(configured_settings())
    prepared = await prepare_test_decision(dispatcher)
    readback = prepared["response_text"]
    transcript_response_id = "resp_transcript"
    completed_response_id = (
        "resp_different" if mismatch == "response_id" else transcript_response_id
    )
    transcript = (
        readback if mismatch == "response_id" else readback + " with an unauthorized scope change"
    )

    dispatcher.note_readback_transcript(transcript_response_id, transcript)
    dispatcher.note_response_done(completed_response_id)
    dispatcher.note_output_audio_stopped(transcript_response_id)

    with pytest.raises(PermissionError, match="readback has not been delivered"):
        await dispatcher.dispatch("arm_owner_verification", {})
    status = await dispatcher.dispatch("check_owner_verification", {})
    assert status.payload["readback_delivered"] is False
    assert status.payload["owner_replied"] is False
    assert status.payload["active"] is False
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#") == []


@pytest.mark.asyncio
async def test_matching_readback_still_requires_a_later_owner_speech_turn() -> None:
    dispatcher, _ = make_dispatcher(configured_settings())
    prepared = await prepare_test_decision(dispatcher)
    readback = prepared["response_text"]

    dispatcher.note_readback_transcript("resp_matching", readback)
    dispatcher.note_response_done("resp_matching")
    dispatcher.note_output_audio_stopped("resp_matching")

    with pytest.raises(PermissionError, match="owner has not replied"):
        await dispatcher.dispatch("arm_owner_verification", {})
    status_before_reply = await dispatcher.dispatch("check_owner_verification", {})
    assert status_before_reply.payload["readback_delivered"] is True
    assert status_before_reply.payload["owner_replied"] is False
    assert status_before_reply.payload["active"] is False

    dispatcher.note_owner_speech_turn()
    armed = await dispatcher.dispatch("arm_owner_verification", {})

    assert armed.payload["armed"] is True
    assert armed.payload["scope"] == "decision"
    assert armed.payload["subject_id"] == prepared["confirmation_id"]
    status_after_reply = await dispatcher.dispatch("check_owner_verification", {})
    assert status_after_reply.payload["active"] is True
    assert status_after_reply.payload["owner_replied"] is True


@pytest.mark.asyncio
async def test_newer_prepare_invalidates_old_readback_reply_and_keypad_state() -> None:
    dispatcher, _ = make_dispatcher(configured_settings())
    first = await prepare_test_decision(
        dispatcher,
        instruction="Approve the first exact deployment.",
    )
    first_window = dispatcher._current_verification()
    assert first_window is not None
    dispatcher.note_readback_transcript(
        "resp_first",
        first["response_text"],
    )
    dispatcher.note_response_done("resp_first")
    dispatcher.note_output_audio_stopped("resp_first")
    dispatcher.note_owner_speech_turn()

    second = await prepare_test_decision(
        dispatcher,
        instruction="Deny the replacement deployment.",
    )

    assert first_window.consumed is True
    current = dispatcher._current_verification()
    assert current is not None
    assert current.subject_id == second["confirmation_id"]
    assert current.subject_id != first["confirmation_id"]
    assert not current.readback_delivered
    assert not current.owner_replied
    assert not current.armed
    with pytest.raises(PermissionError, match="readback has not been delivered"):
        await dispatcher.dispatch("arm_owner_verification", {})
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#") == []


@pytest.mark.asyncio
async def test_conversation_events_drive_readback_reply_arm_and_verification(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    dispatcher, _ = make_dispatcher(settings)
    sockets = FakeSocketFactory()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = dispatcher
    conversation._socket = sockets.socket
    read_task = asyncio.create_task(conversation._read_loop(sockets.socket))

    sockets.socket.push(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_prepare_wired",
                        "name": "prepare_decision",
                        "arguments": json.dumps(
                            {
                                "outcome": "approve",
                                "instruction": "Deploy the event-wired build.",
                                "constraints": ["production only"],
                                "approved_action_ids": [],
                            },
                            separators=(",", ":"),
                        ),
                    }
                ],
            },
        }
    )
    await wait_until(lambda: len(sockets.socket.sent) == 2)
    prepare_output = json.loads(sockets.socket.sent[0]["item"]["output"])
    readback = prepare_output["response_text"]
    assert sockets.socket.sent[1]["type"] == "response.create"
    assert readback in sockets.socket.sent[1]["response"]["instructions"]
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#") == []

    sockets.socket.push(
        {
            "type": "response.output_audio_transcript.done",
            "response_id": "resp_wired_readback",
            "transcript": readback,
        }
    )
    sockets.socket.push(
        {
            "type": "response.done",
            "response": {
                "id": "resp_wired_readback",
                "status": "completed",
                "output": [],
            },
        }
    )
    sockets.socket.push(
        {
            "type": "output_audio_buffer.stopped",
            "response_id": "resp_wired_readback",
        }
    )
    sockets.socket.push({"type": "input_audio_buffer.speech_started"})
    sockets.socket.push({"type": "input_audio_buffer.speech_stopped"})
    sockets.socket.push(
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_arm_wired",
                        "name": "arm_owner_verification",
                        "arguments": "{}",
                    }
                ],
            },
        }
    )
    await wait_until(lambda: len(sockets.socket.sent) == 4)
    arm_output = json.loads(sockets.socket.sent[2]["item"]["output"])
    assert arm_output["ok"] is True
    assert arm_output["armed"] is True
    assert sockets.socket.sent[3]["type"] == "response.create"
    assert sockets.socket.sent[3]["response"]["metadata"] == {
        "hotline_tool_call_id": "call_arm_wired"
    }

    for digit in f"{OWNER_PIN}#":
        sockets.socket.push(
            {
                "type": "input_audio_buffer.dtmf_event_received",
                "event": digit,
            }
        )
    await wait_until(lambda: len(sockets.socket.sent) == 6)

    trusted_signal = sockets.socket.sent[4]["item"]["content"][0]["text"]
    assert "keypad verification succeeded" in trusted_signal
    assert OWNER_PIN not in trusted_signal
    assert OWNER_PIN not in "".join(sockets.socket.sent_raw)
    final_status = await dispatcher.dispatch("check_owner_verification", {})
    assert final_status.payload["active"] is True
    assert final_status.payload["readback_delivered"] is True
    assert final_status.payload["owner_replied"] is True
    assert final_status.payload["verified"] is True

    await sockets.socket.close()
    with pytest.raises(SocketEnded):
        await read_task
    await conversation.stop()


@pytest.mark.asyncio
async def test_wrong_dtmf_attempts_lock_the_window_without_digit_leakage() -> None:
    settings = configured_settings(hotline_voice_pin_max_attempts=2)
    dispatcher, _ = make_dispatcher(settings)
    prepared = await prepare_test_decision(
        dispatcher,
        instruction=f"Never disclose sentinel {OWNER_PIN}.",
    )
    await deliver_readback_reply_and_arm(dispatcher, prepared)

    first = enter_keypad(dispatcher, "000000#")
    second = enter_keypad(dispatcher, "111111#")
    after_lock = enter_keypad(dispatcher, f"{OWNER_PIN}#")
    verification = await dispatcher.dispatch("check_owner_verification", {})

    assert OWNER_PIN not in prepared["response_text"]
    assert len(first) == 1
    assert first[0]["verified"] is False
    assert first[0]["locked"] is False
    assert len(second) == 1
    assert second[0]["verified"] is False
    assert second[0]["locked"] is True
    assert after_lock == []
    assert verification.payload == {
        "ok": True,
        "active": False,
        "prepared": True,
        "readback_delivered": True,
        "owner_replied": True,
        "verified": False,
        "locked": True,
        "scope": "decision",
        "subject_id": prepared["confirmation_id"],
    }
    serialized = json.dumps(
        {
            "prepared": prepared,
            "first": first,
            "second": second,
            "after_lock": after_lock,
            "verification": verification.payload,
        },
        sort_keys=True,
    )
    for digits in (OWNER_PIN, "000000", "111111"):
        assert digits not in serialized


@pytest.mark.asyncio
async def test_pin_attempt_limit_is_global_across_new_windows_and_scopes() -> None:
    settings = configured_settings(hotline_voice_pin_max_attempts=2)
    dispatcher, _ = make_dispatcher(settings)
    decision = await prepare_test_decision(
        dispatcher,
        instruction="Keep this first request safely paused.",
    )
    await deliver_readback_reply_and_arm(dispatcher, decision, response_id="resp_decision")

    first = enter_keypad(dispatcher, "000000#")
    assert first == [
        {
            "verified": False,
            "locked": False,
            "scope": "decision",
            "subject_id": decision["confirmation_id"],
            "message": (
                "Trusted server signal: keypad verification did not match. Ask the owner "
                "to try again using the keypad followed by #. Do not ask them to speak the PIN."
            ),
        }
    ]

    action = await dispatcher.dispatch(
        "prepare_action",
        {
            "action_type": "demo.pause_deployment",
            "parameters": {"reason": "global-pin-budget-test"},
            "workspace_ref": None,
            "thread_id": None,
        },
    )
    await deliver_readback_reply_and_arm(
        dispatcher,
        action.payload,
        response_id="resp_action",
    )
    second = enter_keypad(dispatcher, "111111#")

    assert len(second) == 1
    assert second[0]["verified"] is False
    assert second[0]["locked"] is True
    assert second[0]["scope"] == "action"
    assert second[0]["subject_id"] == action.payload["action_id"]
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#") == []
    with pytest.raises(PermissionError, match="locked for the remainder of this call"):
        await dispatcher.dispatch(
            "prepare_repository_access",
            {"operation": "status"},
        )
    status = await dispatcher.dispatch("check_owner_verification", {})
    assert status.payload["locked"] is True
    assert status.payload["verified"] is False

    serialized = json.dumps(
        {
            "decision": decision,
            "action": action.payload,
            "first": first,
            "second": second,
            "status": status.payload,
        },
        sort_keys=True,
    )
    for digits in (OWNER_PIN, "000000", "111111"):
        assert digits not in serialized


@pytest.mark.asyncio
async def test_sideband_dtmf_event_emits_only_a_digit_free_trusted_signal(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    dispatcher, _ = make_dispatcher(settings)
    prepared = await prepare_test_decision(dispatcher)
    await deliver_readback_reply_and_arm(dispatcher, prepared)
    sockets = FakeSocketFactory()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=RecordingToolCoordinator(),
    )
    conversation.dispatcher = dispatcher
    conversation._socket = sockets.socket
    read_task = asyncio.create_task(conversation._read_loop(sockets.socket))

    for digit in f"{OWNER_PIN}#":
        sockets.socket.push(
            {
                "type": "input_audio_buffer.dtmf_event_received",
                "event": digit,
            }
        )
    await wait_until(lambda: len(sockets.socket.sent) == 2)

    assert sockets.socket.sent[0]["type"] == "conversation.item.create"
    assert sockets.socket.sent[0]["item"]["role"] == "user"
    trusted_text = sockets.socket.sent[0]["item"]["content"][0]["text"]
    assert "keypad verification succeeded" in trusted_text
    assert OWNER_PIN not in trusted_text
    assert OWNER_PIN not in "".join(sockets.socket.sent_raw)
    assert sockets.socket.sent[1]["type"] == "response.create"
    assert sockets.socket.sent[1]["event_id"].startswith("evt_hotline_")

    await sockets.socket.close()
    with pytest.raises(SocketEnded):
        await read_task
    await conversation.stop()


@pytest.mark.asyncio
async def test_decision_requires_fresh_successful_dtmf_and_consumes_it_once() -> None:
    settings = configured_settings()
    dispatcher, coordinator = make_dispatcher(settings)
    prepared = await prepare_test_decision(dispatcher)
    confirmation_id = prepared["confirmation_id"]

    with pytest.raises(PermissionError, match="keypad verification"):
        await dispatcher.dispatch(
            "record_decision",
            {"confirmation_id": confirmation_id},
        )

    await deliver_readback_reply_and_arm(dispatcher, prepared)
    statuses = enter_keypad(dispatcher, f"{OWNER_PIN}#")
    assert len(statuses) == 1
    assert statuses[0]["verified"] is True
    assert OWNER_PIN not in json.dumps(statuses)

    result = await dispatcher.dispatch(
        "record_decision",
        {"confirmation_id": confirmation_id},
    )

    assert result.payload["ok"] is True
    assert result.payload["accepted"] is True
    assert [name for name, _ in coordinator.calls] == ["record_instruction"]
    request = cast(Any, coordinator.calls[0][1])
    assert request.event_id == "evt_dispatch"
    assert request.outcome == "approve"
    assert request.confirmation_method == "spoken_plus_dtmf"
    assert request.confirmation_pin.get_secret_value() == OWNER_PIN
    assert OWNER_PIN not in repr(request)
    assert "confirmation_pin" not in request.model_dump(mode="json")

    with pytest.raises(PermissionError, match="keypad verification"):
        await dispatcher.dispatch(
            "record_decision",
            {"confirmation_id": confirmation_id},
        )


@pytest.mark.asyncio
async def test_dtmf_entered_before_readback_cannot_verify_a_prepared_request() -> None:
    """A prepare response is not proof that its readback reached the owner."""

    dispatcher, _ = make_dispatcher(configured_settings())
    await prepare_test_decision(dispatcher)

    statuses = enter_keypad(dispatcher, f"{OWNER_PIN}#")

    assert statuses == []


@pytest.mark.asyncio
async def test_successful_verification_window_ignores_all_later_keypad_input() -> None:
    dispatcher, _ = make_dispatcher(configured_settings())
    prepared = await prepare_test_decision(dispatcher)
    await deliver_readback_reply_and_arm(dispatcher, prepared)
    success = enter_keypad(dispatcher, f"{OWNER_PIN}#")
    assert success and success[0]["verified"] is True

    later = enter_keypad(dispatcher, "000000#")

    assert later == []


@pytest.mark.asyncio
async def test_action_prepare_verify_confirm_execute_sequence_is_bound_and_one_time() -> None:
    settings = configured_settings()
    dispatcher, coordinator = make_dispatcher(settings)
    prepared = await dispatcher.dispatch(
        "prepare_action",
        {
            "action_type": "demo.pause_deployment",
            "parameters": {"reason": "incident-response"},
            "workspace_ref": None,
            "thread_id": None,
        },
    )
    action_id = prepared.payload["action_id"]

    assert prepared.payload["require_repeat_verbatim"] is True
    assert prepared.payload["pin_instruction"].endswith("never ask for spoken digits.")
    with pytest.raises(PermissionError, match="keypad verification"):
        await dispatcher.dispatch(
            "confirm_action",
            {
                "action_id": action_id,
                "exact_confirmation": prepared.payload["exact_readback"],
            },
        )

    await deliver_readback_reply_and_arm(dispatcher, prepared.payload)
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#")[0]["verified"] is True
    confirmed = await dispatcher.dispatch(
        "confirm_action",
        {
            "action_id": action_id,
            "exact_confirmation": prepared.payload["exact_readback"],
        },
    )
    assert confirmed.payload["confirmed"] is True
    assert confirmed.payload["grant_id"] == "grt_realtime_test"

    executed = await dispatcher.dispatch(
        "execute_action",
        {"action_id": action_id},
    )
    assert executed.payload["executed"] is True
    assert executed.payload["operation_id"] == "op_realtime_test"
    assert [name for name, _ in coordinator.calls] == [
        "prepare_action",
        "confirm_action",
        "execute_action",
    ]

    with pytest.raises(PermissionError, match="already consumed"):
        await dispatcher.dispatch(
            "execute_action",
            {"action_id": action_id},
        )


@pytest.mark.asyncio
async def test_execute_action_timeout_after_consumption_records_unknown_and_returns_it(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configured_settings()
    event = EscalationEvent(
        kind="approval",
        summary="A bounded demo action needs owner authorization.",
        question="Should the exact demo action run?",
    )
    await store.create_event(event)
    await store.transition_event(event.event_id, EventState.QUEUED)
    await store.transition_event(event.event_id, EventState.DIALING)
    await store.transition_event(event.event_id, EventState.CONNECTED)
    session = await store.create_session(
        ContactSession(
            event_id=event.event_id,
            direction=ContactDirection.OUTBOUND_ESCALATION,
            state=SessionState.CONNECTED,
            attempt_id=TWILIO_CALL_SID,
            interaction_id="call_timed_out_action",
            provider="openai_realtime",
        )
    )
    coordinator = real_coordinator(settings, store)
    dispatcher = RealtimeToolDispatcher(
        settings=settings,
        coordinator=coordinator,
        event_id=event.event_id,
        session_id=session.session_id,
        direction="outbound_escalation",
    )
    prepared = await dispatcher.dispatch(
        "prepare_action",
        {
            "action_type": "demo.pause_deployment",
            "parameters": {"reason": "incident-response"},
            "workspace_ref": None,
            "thread_id": None,
        },
    )
    action_id = prepared.payload["action_id"]
    await deliver_readback_reply_and_arm(dispatcher, prepared.payload)
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#")[0]["verified"] is True
    exact_phrase = prepared.payload["exact_readback"].rsplit("say exactly: ", 1)[1]
    confirmed = await dispatcher.dispatch(
        "confirm_action",
        {
            "action_id": action_id,
            "exact_confirmation": exact_phrase,
        },
    )
    assert confirmed.payload["confirmed"] is True

    execution_started = asyncio.Event()
    execution_calls = 0

    async def consume_then_block(request: object) -> ExecuteActionResponse:
        nonlocal execution_calls
        execution_calls += 1
        action = await store.get_action(cast(Any, request).action_id)
        assert action is not None
        await store.consume_action(
            cast(Any, request).grant_id,
            action_hash=action.action_hash,
        )
        execution_started.set()
        await asyncio.Event().wait()
        raise AssertionError("the timed-out execution must be cancelled")

    monkeypatch.setattr(coordinator, "execute_action", consume_then_block)
    monkeypatch.setattr(realtime, "_TOOL_EXECUTION_TIMEOUT_SECONDS", 0.2)
    sockets = FakeSocketFactory()
    conversation = build_conversation(
        settings=settings,
        store=store,
        client=FakeControlClient(),
        socket_factory=sockets,
        coordinator=coordinator,
        call_id="call_timed_out_action",
    )
    conversation.dispatcher = dispatcher

    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_execute_timed_out_action",
        name="execute_action",
        arguments_json=json.dumps({"action_id": action_id}),
    )

    assert execution_started.is_set()
    assert execution_calls == 1
    output = json.loads(sockets.socket.sent[0]["item"]["output"])
    assert output["ok"] is True
    assert output["executed"] is False
    assert "outcome is unknown" in output["message_to_user"]
    receipt = await store.get_action_execution(action_id)
    assert receipt is not None
    assert receipt.kind is TimelineKind.ACTION_EXECUTION_UNKNOWN
    assert receipt.details["status"] == "unknown"
    sockets.socket.sent.clear()
    sockets.socket.sent_raw.clear()

    await process_tool_call_with_server_acks(
        conversation,
        sockets.socket,
        tool_call_id="call_execute_timed_out_action",
        name="execute_action",
        arguments_json=json.dumps({"action_id": action_id}),
    )

    assert execution_calls == 1
    assert sockets.socket.sent == []


@pytest.mark.asyncio
async def test_repository_query_requires_fresh_pin_then_blocks_later_authority() -> None:
    settings = configured_settings()
    dispatcher, coordinator = make_dispatcher(settings)
    prepared = await dispatcher.dispatch(
        "prepare_repository_access",
        {
            "workspace": None,
            "operation": "status",
            "query": None,
            "path": None,
            "line_start": 1,
            "line_count": 20,
            "max_results": 10,
        },
    )
    request_id = prepared.payload["request_id"]

    assert "evidence-only" in prepared.payload["response_text"]
    with pytest.raises(PermissionError, match="keypad verification"):
        await dispatcher.dispatch(
            "query_repository_context",
            {"request_id": request_id},
        )

    await deliver_readback_reply_and_arm(dispatcher, prepared.payload)
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#")[0]["verified"] is True
    evidence = await dispatcher.dispatch(
        "query_repository_context",
        {"request_id": request_id},
    )
    assert evidence.payload["untrusted_data"] is True
    assert evidence.payload["operation"] == "status"
    assert coordinator.evidence_exposed

    decision = await prepare_test_decision(
        dispatcher,
        instruction="Approve based on repository evidence.",
    )
    await deliver_readback_reply_and_arm(
        dispatcher,
        decision,
        response_id="resp_later_decision",
    )
    assert enter_keypad(dispatcher, f"{OWNER_PIN}#")[0]["verified"] is True
    with pytest.raises(PermissionError, match="non-authoritative"):
        await dispatcher.dispatch(
            "record_decision",
            {"confirmation_id": decision["confirmation_id"]},
        )


@pytest.mark.asyncio
async def test_manager_shutdown_hangs_up_closes_socket_and_cancels_worker(
    store: SQLiteStore,
) -> None:
    settings = configured_settings()
    client = FakeControlClient()
    sockets = FakeSocketFactory()
    provider = FakeCallProvider()
    manager = OpenAIRealtimeManager(
        settings=settings,
        store=store,
        coordinator=real_coordinator(settings, store, provider=provider),
        client=cast(Any, client),
        socket_factory=sockets,
    )
    result = await manager.handle_webhook(
        webhook_bytes(
            await admitted_incoming_webhook(
                store,
                call_id="call_shutdown",
            )
        ),
        verified_headers("wh_shutdown"),
    )
    assert result.accepted
    await wait_until(lambda: manager.active_calls == 1 and bool(sockets.connections))

    await manager.close()
    await manager.close()

    assert manager.active_calls == 0
    assert client.hung_up == ["call_shutdown"]
    assert provider.terminated_attempts == [TWILIO_CALL_SID]
    assert sockets.socket.closed
    assert sockets.socket.close_reason == "call ended"
    assert manager._workers == {}
    assert manager._conversations == {}


@pytest.mark.asyncio
async def test_manager_closes_the_call_control_client_it_constructs(
    store: SQLiteStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeControlClient()
    monkeypatch.setattr(
        realtime,
        "OpenAIRealtimeClient",
        lambda _settings: client,
    )
    manager = OpenAIRealtimeManager(
        settings=configured_settings(),
        store=store,
        coordinator=real_coordinator(configured_settings(), store),
        socket_factory=FakeSocketFactory(),
    )

    await manager.close()

    assert client.closed
