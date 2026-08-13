from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode
from xml.etree import ElementTree

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from agent_hotline.api import create_app
from agent_hotline.openai_realtime import (
    IncomingCallResult,
    OpenAIRealtimeError,
    OpenAIWebhookVerificationError,
)
from agent_hotline.providers import FakeCallProvider
from agent_hotline.settings import Settings
from agent_hotline.telephony_security import (
    build_correlation_signature,
    verify_correlation_signature,
)
from agent_hotline.twilio import (
    build_outbound_voice_event_signature,
    compute_twilio_webhook_signature,
    compute_twilio_websocket_signature,
)

PUBLIC_BASE_URL = "https://hotline.example.test"
OPENAI_API_KEY = "sk-openai-realtime-api-test-secret"
OPENAI_WEBHOOK_SECRET = "whsec-openai-realtime-api-test-secret"
OPENAI_PROJECT_ID = "proj_hotline_api_test"
TWILIO_ACCOUNT_SID = "AC" + ("a" * 32)
TWILIO_AUTH_TOKEN = "twilio-auth-token-for-api-tests"
TWILIO_PHONE_NUMBER = "+12025550100"
TWILIO_CALL_SID = "CA" + ("b" * 32)
OWNER_PHONE_NUMBER = "+12025550199"
OWNER_PIN = "246810"
CALLBACK_TOKEN = "sip-correlation-signing-token-for-api-tests-123456"
OPENAI_WEBHOOK_PATH = "/v1/openai/realtime/webhook"
TWILIO_INBOUND_PATH = "/v1/twilio/voice/incoming"
TWILIO_OUTBOUND_PATH = "/v1/twilio/voice/outbound"
TWILIO_STATUS_PATH = "/v1/twilio/status"
TWILIO_MEDIA_PATH = "/v1/twilio/media"

SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


def configured_settings(database_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "hotline_env": "test",
        "hotline_database_path": database_path,
        "hotline_transport": "openai_realtime",
        "openai_api_key": SecretStr(OPENAI_API_KEY),
        "openai_webhook_secret": SecretStr(OPENAI_WEBHOOK_SECRET),
        "openai_project_id": OPENAI_PROJECT_ID,
        "public_base_url": PUBLIC_BASE_URL,
        "hotline_local_token": SecretStr("local-realtime-service-token-for-api-tests-123456"),
        "hotline_sip_correlation_secret": SecretStr(CALLBACK_TOKEN),
        "hotline_action_signing_secret": SecretStr("action-signing-token-for-api-tests-1234567890"),
        "hotline_fallback_signing_secret": SecretStr(
            "fallback-signing-token-for-api-tests-1234567890"
        ),
        "owner_confirmation_pin": SecretStr(OWNER_PIN),
        "owner_phone_number": SecretStr(OWNER_PHONE_NUMBER),
        "twilio_account_sid": TWILIO_ACCOUNT_SID,
        "twilio_auth_token": SecretStr(TWILIO_AUTH_TOKEN),
        "twilio_phone_number": TWILIO_PHONE_NUMBER,
        "hotline_allowlisted_callers": OWNER_PHONE_NUMBER,
        "hotline_retry_attempts": 0,
        "codex_app_server_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


class FakeRealtimeManager:
    def __init__(self) -> None:
        self.active_calls = 3
        self.webhook_result = IncomingCallResult(
            handled=False,
            accepted=False,
            duplicate=False,
        )
        self.webhook_error: Exception | None = None
        self.status_result: dict[str, Any] = {
            "accepted": True,
            "terminal": False,
        }
        self.status_error: Exception | None = None
        self.webhook_calls: list[tuple[bytes, dict[str, str]]] = []
        self.status_calls: list[dict[str, str | None]] = []
        self.outbound_bind_calls: list[dict[str, str]] = []
        self.outbound_should_dial = True
        self.outbound_bind_error: Exception | None = None
        self.media_stream_calls: list[Any] = []
        self.close_calls = 0

    async def handle_webhook(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
    ) -> IncomingCallResult:
        self.webhook_calls.append((raw_body, dict(headers)))
        if self.webhook_error is not None:
            raise self.webhook_error
        return self.webhook_result

    async def handle_carrier_status(
        self,
        *,
        call_sid: str,
        status: str,
        receipt_id: str | None = None,
        correlated_event_id: str | None = None,
    ) -> dict[str, Any]:
        call = {
            "call_sid": call_sid,
            "status": status,
            "receipt_id": receipt_id,
        }
        if correlated_event_id is not None:
            call["correlated_event_id"] = correlated_event_id
        self.status_calls.append(call)
        if self.status_error is not None:
            raise self.status_error
        return dict(self.status_result)

    async def bind_outbound_carrier_parent(
        self,
        *,
        event_id: str,
        call_sid: str,
    ) -> tuple[object, bool]:
        self.outbound_bind_calls.append(
            {
                "event_id": event_id,
                "call_sid": call_sid,
            }
        )
        if self.outbound_bind_error is not None:
            raise self.outbound_bind_error
        return object(), self.outbound_should_dial

    async def close(self) -> None:
        self.close_calls += 1

    async def run_media_stream(self, stream: Any) -> None:
        self.media_stream_calls.append(stream.start)


@dataclass(slots=True)
class RealtimeAPIHarness:
    app: FastAPI
    client: httpx.AsyncClient
    manager: FakeRealtimeManager
    settings: Settings


@pytest_asyncio.fixture
async def realtime_api(tmp_path: Path) -> AsyncIterator[RealtimeAPIHarness]:
    settings = configured_settings(tmp_path / "realtime-api.sqlite3")
    manager = FakeRealtimeManager()
    app = create_app(
        settings=settings,
        provider=FakeCallProvider(),
        realtime_manager=manager,  # type: ignore[arg-type]
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield RealtimeAPIHarness(
                app=app,
                client=client,
                manager=manager,
                settings=settings,
            )


def signed_twilio_form(
    path: str,
    pairs: list[tuple[str, str]],
) -> tuple[bytes, dict[str, str]]:
    body = urlencode(pairs).encode("ascii")
    signature = compute_twilio_webhook_signature(
        url=f"{PUBLIC_BASE_URL}{path}",
        params=pairs,
        auth_token=TWILIO_AUTH_TOKEN,
    )
    return body, {
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "X-Twilio-Signature": signature,
    }


def assert_security_headers(response: httpx.Response) -> None:
    for name, expected in SECURITY_HEADERS.items():
        assert response.headers.get(name) == expected


@pytest.mark.asyncio
async def test_production_lifespan_fails_before_resources_for_incomplete_realtime(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "must-not-be-created.sqlite3"
    app = create_app(
        settings=Settings(
            _env_file=None,
            hotline_database_path=database_path,
            hotline_transport="openai_realtime",
            codex_app_server_enabled=False,
        )
    )

    with pytest.raises(RuntimeError, match="requires complete OpenAI"):
        async with app.router.lifespan_context(app):
            pytest.fail("incomplete Realtime startup must not yield")

    assert not database_path.exists()


@pytest.mark.asyncio
async def test_openai_webhook_forwards_exact_raw_body_and_headers(
    realtime_api: RealtimeAPIHarness,
) -> None:
    raw_body = (
        '{\n  "type": "response.completed",\n  "data": {"note": "exact bytes ☃"}\n}\n'
    ).encode()
    response = await realtime_api.client.post(
        OPENAI_WEBHOOK_PATH,
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "Webhook-Id": "wh_exact_123",
            "Webhook-Timestamp": "1750287078",
            "Webhook-Signature": "v1,exact-test-signature",
            "X-Exact-Test": "preserve-this-value",
        },
    )

    assert response.status_code == 200
    assert len(realtime_api.manager.webhook_calls) == 1
    forwarded_body, forwarded_headers = realtime_api.manager.webhook_calls[0]
    assert forwarded_body == raw_body
    assert forwarded_headers == dict(response.request.headers.items())
    assert response.json() == {
        "handled": False,
        "accepted": False,
        "duplicate": False,
        "call_id": None,
        "event_id": None,
        "reason": None,
    }
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_realtime_runtime_does_not_expose_removed_legacy_or_hackathon_routes(
    realtime_api: RealtimeAPIHarness,
) -> None:
    public_paths = {
        route.path
        for route in realtime_api.app.routes
        if isinstance(getattr(route, "path", None), str)
    }

    assert not any(
        marker in path.casefold()
        for path in public_paths
        for marker in ("sarvam", "samvaad", "hackathon", "registration")
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            IncomingCallResult(
                handled=False,
                accepted=False,
                duplicate=False,
            ),
            {
                "handled": False,
                "accepted": False,
                "duplicate": False,
                "call_id": None,
                "event_id": None,
                "reason": None,
            },
        ),
        (
            IncomingCallResult(
                handled=True,
                accepted=True,
                duplicate=False,
                call_id="call_verified_123",
                event_id="evt_verified_123",
            ),
            {
                "handled": True,
                "accepted": True,
                "duplicate": False,
                "call_id": "call_verified_123",
                "event_id": "evt_verified_123",
                "reason": None,
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_openai_webhook_returns_verified_unknown_or_accepted_result(
    realtime_api: RealtimeAPIHarness,
    result: IncomingCallResult,
    expected: dict[str, Any],
) -> None:
    realtime_api.manager.webhook_result = result

    response = await realtime_api.client.post(
        OPENAI_WEBHOOK_PATH,
        content=b'{"type":"verified-test-event"}',
        headers={
            "Content-Type": "application/json",
            "Webhook-Id": "wh_verified_result",
            "Webhook-Timestamp": "1750287078",
            "Webhook-Signature": "v1,verified-test-signature",
        },
    )

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.asyncio
async def test_openai_webhook_maps_verification_failure_to_bad_request(
    realtime_api: RealtimeAPIHarness,
) -> None:
    realtime_api.manager.webhook_error = OpenAIWebhookVerificationError(
        "webhook signature is invalid"
    )

    response = await realtime_api.client.post(
        OPENAI_WEBHOOK_PATH,
        content=b'{"type":"realtime.call.incoming"}',
        headers={
            "Content-Type": "application/json",
            "Webhook-Id": "wh_invalid",
            "Webhook-Timestamp": "1750287078",
            "Webhook-Signature": "v1,invalid",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "webhook signature is invalid"}
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_openai_webhook_maps_manager_failure_without_leaking_detail(
    realtime_api: RealtimeAPIHarness,
) -> None:
    sensitive_detail = "provider failed with sk-do-not-leak and +12025550199"
    realtime_api.manager.webhook_error = OpenAIRealtimeError(sensitive_detail)

    response = await realtime_api.client.post(
        OPENAI_WEBHOOK_PATH,
        content=b'{"type":"realtime.call.incoming"}',
        headers={
            "Content-Type": "application/json",
            "Webhook-Id": "wh_manager_failure",
            "Webhook-Timestamp": "1750287078",
            "Webhook-Signature": "v1,valid-but-manager-failed",
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Realtime call admission is temporarily unavailable"}
    assert sensitive_detail not in response.text
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_twilio_inbound_accepts_valid_signature_and_returns_exact_signed_xml(
    realtime_api: RealtimeAPIHarness,
) -> None:
    body, headers = signed_twilio_form(
        TWILIO_INBOUND_PATH,
        [
            ("CallSid", TWILIO_CALL_SID),
            ("AccountSid", TWILIO_ACCOUNT_SID),
            ("From", OWNER_PHONE_NUMBER),
            ("To", TWILIO_PHONE_NUMBER),
            ("Direction", "inbound"),
        ],
    )

    response = await realtime_api.client.post(
        TWILIO_INBOUND_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(response.content)
    assert root.tag == "Response"
    dial = root.find("./Dial")
    assert dial is not None
    assert dial.attrib == {
        "timeLimit": "1800",
        "timeout": "30",
        "action": f"{PUBLIC_BASE_URL}{TWILIO_STATUS_PATH}",
        "method": "POST",
    }
    sip = root.find("./Dial/Sip")
    assert sip is not None
    assert sip.text is not None
    base_uri, separator, query = sip.text.partition("?")
    assert separator == "?"
    assert base_uri == (f"sip:{OPENAI_PROJECT_ID}@sip.api.openai.com;transport=tls")
    sip_headers = dict(parse_qsl(query, strict_parsing=True))
    assert sip_headers.keys() == {
        "X-Hotline-Direction",
        "X-Hotline-Call-Sid",
        "X-Hotline-Caller",
        "X-Hotline-Admission",
        "X-Hotline-Expires",
        "X-Hotline-Signature",
    }
    assert sip_headers["X-Hotline-Direction"] == "inbound"
    assert sip_headers["X-Hotline-Call-Sid"] == TWILIO_CALL_SID
    assert sip_headers["X-Hotline-Caller"] == OWNER_PHONE_NUMBER
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="inbound",
        call_sid=TWILIO_CALL_SID,
        event_id=None,
        caller_phone=OWNER_PHONE_NUMBER,
        admission_nonce=sip_headers["X-Hotline-Admission"],
        expires_at_epoch=int(sip_headers["X-Hotline-Expires"]),
        signature=sip_headers["X-Hotline-Signature"],
    )
    assert CALLBACK_TOKEN not in response.text
    assert response.text == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Response><Dial timeLimit="1800" timeout="30" '
        f'action="{PUBLIC_BASE_URL}{TWILIO_STATUS_PATH}" method="POST">'
        f"<Sip>sip:{OPENAI_PROJECT_ID}@sip.api.openai.com;"
        "transport=tls?X-Hotline-Direction=inbound&amp;"
        f"X-Hotline-Call-Sid={TWILIO_CALL_SID}&amp;"
        f"X-Hotline-Caller=%2B12025550199&amp;"
        f"X-Hotline-Admission={sip_headers['X-Hotline-Admission']}&amp;"
        f"X-Hotline-Expires={sip_headers['X-Hotline-Expires']}&amp;"
        f"X-Hotline-Signature={sip_headers['X-Hotline-Signature']}"
        "</Sip></Dial></Response>"
    )
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_twilio_outbound_route_binds_parent_idempotently_and_stops_terminal_dial(
    realtime_api: RealtimeAPIHarness,
) -> None:
    event_id = "evt_outbound_twiml_route"
    event_signature = build_outbound_voice_event_signature(
        CALLBACK_TOKEN,
        event_id=event_id,
    )
    path = f"{TWILIO_OUTBOUND_PATH}?" + urlencode(
        {
            "event_id": event_id,
            "event_sig": event_signature,
        }
    )
    pairs = [
        ("CallSid", TWILIO_CALL_SID),
        ("AccountSid", TWILIO_ACCOUNT_SID),
        ("From", TWILIO_PHONE_NUMBER),
        ("To", OWNER_PHONE_NUMBER),
        ("Direction", "outbound-api"),
    ]
    body, headers = signed_twilio_form(path, pairs)

    first = await realtime_api.client.post(path, content=body, headers=headers)
    replay = await realtime_api.client.post(path, content=body, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.text == first.text
    root = ElementTree.fromstring(first.content)
    dial = root.find("./Dial")
    sip = root.find("./Dial/Sip")
    assert dial is not None
    assert dial.attrib == {"timeLimit": "1800", "timeout": "30"}
    assert sip is not None
    assert sip.text is not None
    _base_uri, separator, query = sip.text.partition("?")
    assert separator == "?"
    sip_headers = dict(parse_qsl(query, strict_parsing=True))
    assert sip_headers["X-Hotline-Direction"] == "outbound"
    assert sip_headers["X-Hotline-Call-Sid"] == TWILIO_CALL_SID
    assert sip_headers["X-Hotline-Event-Id"] == event_id
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=TWILIO_CALL_SID,
        event_id=event_id,
        signature=sip_headers["X-Hotline-Signature"],
    )
    assert realtime_api.manager.outbound_bind_calls == [
        {"event_id": event_id, "call_sid": TWILIO_CALL_SID},
        {"event_id": event_id, "call_sid": TWILIO_CALL_SID},
    ]

    realtime_api.manager.outbound_should_dial = False
    terminal = await realtime_api.client.post(path, content=body, headers=headers)

    assert terminal.status_code == 200
    assert terminal.text == '<?xml version="1.0" encoding="UTF-8"?><Response />'
    assert realtime_api.manager.outbound_bind_calls[-1] == {
        "event_id": event_id,
        "call_sid": TWILIO_CALL_SID,
    }
    assert CALLBACK_TOKEN not in first.text + replay.text + terminal.text
    assert_security_headers(first)
    assert_security_headers(terminal)


@pytest.mark.asyncio
async def test_twilio_outbound_media_mode_returns_authenticated_bidirectional_stream(
    realtime_api: RealtimeAPIHarness,
) -> None:
    realtime_api.settings.twilio_bridge_mode = "media_stream"
    event_id = "evt_outbound_media_route"
    event_signature = build_outbound_voice_event_signature(
        CALLBACK_TOKEN,
        event_id=event_id,
    )
    path = f"{TWILIO_OUTBOUND_PATH}?" + urlencode(
        {"event_id": event_id, "event_sig": event_signature}
    )
    pairs = [
        ("CallSid", TWILIO_CALL_SID),
        ("AccountSid", TWILIO_ACCOUNT_SID),
        ("From", TWILIO_PHONE_NUMBER),
        ("To", OWNER_PHONE_NUMBER),
        ("Direction", "outbound-api"),
    ]
    body, headers = signed_twilio_form(path, pairs)

    response = await realtime_api.client.post(path, content=body, headers=headers)

    assert response.status_code == 200
    stream = ElementTree.fromstring(response.content).find("./Connect/Stream")
    assert stream is not None
    assert stream.attrib == {"url": f"wss://hotline.example.test{TWILIO_MEDIA_PATH}"}
    parameters = {
        item.attrib["name"]: item.attrib["value"] for item in stream.findall("./Parameter")
    }
    assert parameters["HotlineEventId"] == event_id
    assert parameters["HotlineCallSid"] == TWILIO_CALL_SID
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=TWILIO_CALL_SID,
        event_id=event_id,
        signature=parameters["HotlineSignature"],
    )


@pytest.mark.asyncio
async def test_twilio_inbound_media_mode_issues_idempotent_call_bound_admission(
    realtime_api: RealtimeAPIHarness,
) -> None:
    realtime_api.settings.twilio_bridge_mode = "media_stream"
    pairs = [
        ("CallSid", TWILIO_CALL_SID),
        ("AccountSid", TWILIO_ACCOUNT_SID),
        ("From", OWNER_PHONE_NUMBER),
        ("To", TWILIO_PHONE_NUMBER),
        ("Direction", "inbound"),
    ]
    body, headers = signed_twilio_form(TWILIO_INBOUND_PATH, pairs)

    first = await realtime_api.client.post(TWILIO_INBOUND_PATH, content=body, headers=headers)
    replay = await realtime_api.client.post(TWILIO_INBOUND_PATH, content=body, headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.text == first.text
    stream = ElementTree.fromstring(first.content).find("./Connect/Stream")
    assert stream is not None
    assert stream.attrib == {"url": f"wss://hotline.example.test{TWILIO_MEDIA_PATH}"}
    parameters = {
        item.attrib["name"]: item.attrib["value"] for item in stream.findall("./Parameter")
    }
    assert await realtime_api.app.state.store.get_session_by_interaction(TWILIO_CALL_SID) is None
    assert parameters["HotlineDirection"] == "inbound"
    assert "HotlineEventId" not in parameters
    expires_at_epoch = int(parameters["HotlineExpires"])
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="inbound",
        call_sid=TWILIO_CALL_SID,
        event_id=None,
        caller_phone=OWNER_PHONE_NUMBER,
        admission_nonce=parameters["HotlineAdmission"],
        expires_at_epoch=expires_at_epoch,
        signature=parameters["HotlineSignature"],
    )
    await realtime_api.app.state.store.consume_carrier_admission(
        TWILIO_CALL_SID,
        caller_phone=OWNER_PHONE_NUMBER,
        admission_nonce=parameters["HotlineAdmission"],
        expires_at_epoch=expires_at_epoch,
        provider_call_id=TWILIO_CALL_SID,
    )
    assert_security_headers(first)


def test_twilio_media_websocket_verifies_handshake_and_call_correlation(tmp_path: Path) -> None:
    settings = configured_settings(
        tmp_path / "twilio-media-api.sqlite3",
        twilio_bridge_mode="media_stream",
    )
    manager = FakeRealtimeManager()
    app = create_app(
        settings=settings,
        provider=FakeCallProvider(),
        realtime_manager=manager,  # type: ignore[arg-type]
    )
    signature = compute_twilio_websocket_signature(
        url=f"wss://hotline.example.test{TWILIO_MEDIA_PATH}",
        params=None,
        auth_token=TWILIO_AUTH_TOKEN,
    )
    correlation = build_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=TWILIO_CALL_SID,
        event_id="evt_media_websocket",
    )
    stream_sid = "MZ" + ("c" * 32)
    with (
        TestClient(app) as client,
        client.websocket_connect(
            TWILIO_MEDIA_PATH,
            headers={"x-twilio-signature": signature},
        ) as websocket,
    ):
        websocket.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        websocket.send_json(
            {
                "event": "start",
                "sequenceNumber": "1",
                "streamSid": stream_sid,
                "start": {
                    "accountSid": TWILIO_ACCOUNT_SID,
                    "callSid": TWILIO_CALL_SID,
                    "streamSid": stream_sid,
                    "tracks": ["inbound"],
                    "mediaFormat": {
                        "encoding": "audio/x-mulaw",
                        "sampleRate": 8000,
                        "channels": 1,
                    },
                    "customParameters": {
                        "HotlineDirection": "outbound",
                        "HotlineCallSid": TWILIO_CALL_SID,
                        "HotlineEventId": "evt_media_websocket",
                        "HotlineSignature": correlation,
                    },
                },
            }
        )
        websocket.receive()

    assert len(manager.media_stream_calls) == 1
    assert manager.media_stream_calls[0].call_sid == TWILIO_CALL_SID
    assert manager.media_stream_calls[0].event_id == "evt_media_websocket"
    assert manager.media_stream_calls[0].direction == "outbound"


def test_twilio_media_websocket_is_closed_when_bridge_mode_is_sip(tmp_path: Path) -> None:
    settings = configured_settings(tmp_path / "twilio-media-disabled.sqlite3")
    app = create_app(
        settings=settings,
        provider=FakeCallProvider(),
        realtime_manager=FakeRealtimeManager(),  # type: ignore[arg-type]
    )

    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as disconnected,
        client.websocket_connect(TWILIO_MEDIA_PATH),
    ):
        pass

    assert disconnected.value.code == 1008


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AccountSid", "AC" + ("c" * 32)),
        ("From", "+12025550888"),
        ("To", "+12025550777"),
        ("Direction", "outbound-api"),
    ],
)
@pytest.mark.asyncio
async def test_twilio_inbound_rejects_signed_but_wrong_call_scope(
    realtime_api: RealtimeAPIHarness,
    field: str,
    value: str,
) -> None:
    values = {
        "CallSid": TWILIO_CALL_SID,
        "AccountSid": TWILIO_ACCOUNT_SID,
        "From": OWNER_PHONE_NUMBER,
        "To": TWILIO_PHONE_NUMBER,
        "Direction": "inbound",
    }
    values[field] = value
    body, headers = signed_twilio_form(
        TWILIO_INBOUND_PATH,
        list(values.items()),
    )

    response = await realtime_api.client.post(
        TWILIO_INBOUND_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Twilio call is not authorized"}
    assert_security_headers(response)


@pytest.mark.parametrize("signature_mode", ["invalid", "missing"])
@pytest.mark.asyncio
async def test_twilio_inbound_rejects_invalid_or_missing_signature(
    realtime_api: RealtimeAPIHarness,
    signature_mode: str,
) -> None:
    body, headers = signed_twilio_form(
        TWILIO_INBOUND_PATH,
        [("CallSid", TWILIO_CALL_SID)],
    )
    if signature_mode == "invalid":
        headers["X-Twilio-Signature"] = "invalid-signature"
    else:
        del headers["X-Twilio-Signature"]

    response = await realtime_api.client.post(
        TWILIO_INBOUND_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Twilio webhook signature is invalid"}
    assert_security_headers(response)


@pytest.mark.asyncio
async def test_twilio_inbound_rejects_duplicate_call_sid_after_valid_signature(
    realtime_api: RealtimeAPIHarness,
) -> None:
    pairs = [
        ("CallSid", TWILIO_CALL_SID),
        ("CallSid", TWILIO_CALL_SID),
    ]
    body, headers = signed_twilio_form(TWILIO_INBOUND_PATH, pairs)

    response = await realtime_api.client.post(
        TWILIO_INBOUND_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Twilio CallSid is missing or duplicated"}


@pytest.mark.asyncio
async def test_twilio_inbound_rejects_wrong_content_type(
    realtime_api: RealtimeAPIHarness,
) -> None:
    response = await realtime_api.client.post(
        TWILIO_INBOUND_PATH,
        content=b'{"CallSid":"' + TWILIO_CALL_SID.encode() + b'"}',
        headers={
            "Content-Type": "application/json",
            "X-Twilio-Signature": "irrelevant-before-content-type-validation",
        },
    )

    assert response.status_code == 415
    assert response.json() == {"detail": "Twilio webhook must be form encoded"}


@pytest.mark.asyncio
async def test_twilio_status_verifies_signature_and_passes_deterministic_receipt(
    realtime_api: RealtimeAPIHarness,
) -> None:
    pairs = [
        ("CallStatus", "ringing"),
        ("CallSid", TWILIO_CALL_SID),
        ("SequenceNumber", "3"),
        ("Timestamp", "Thu, 30 Jul 2026 10:00:00 +0000"),
    ]
    body, headers = signed_twilio_form(TWILIO_STATUS_PATH, pairs)
    expected_receipt = hashlib.sha256(
        json.dumps(
            pairs,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:48]

    first = await realtime_api.client.post(
        TWILIO_STATUS_PATH,
        content=body,
        headers=headers,
    )
    second = await realtime_api.client.post(
        TWILIO_STATUS_PATH,
        content=body,
        headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == {"accepted": True, "terminal": False}
    assert second.json() == first.json()
    assert realtime_api.manager.status_calls == [
        {
            "call_sid": TWILIO_CALL_SID,
            "status": "ringing",
            "receipt_id": expected_receipt,
        },
        {
            "call_sid": TWILIO_CALL_SID,
            "status": "ringing",
            "receipt_id": expected_receipt,
        },
    ]
    assert_security_headers(first)


@pytest.mark.asyncio
async def test_twilio_dial_action_status_takes_precedence_over_parent_call_status(
    realtime_api: RealtimeAPIHarness,
) -> None:
    pairs = [
        ("CallSid", TWILIO_CALL_SID),
        ("CallStatus", "in-progress"),
        ("DialCallStatus", "completed"),
    ]
    body, headers = signed_twilio_form(TWILIO_STATUS_PATH, pairs)

    response = await realtime_api.client.post(
        TWILIO_STATUS_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/xml")
    assert response.text == '<?xml version="1.0" encoding="UTF-8"?><Response />'
    assert realtime_api.manager.status_calls == [
        {
            "call_sid": TWILIO_CALL_SID,
            "status": "completed",
            "receipt_id": hashlib.sha256(
                json.dumps(
                    pairs,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()[:48],
        }
    ]


@pytest.mark.parametrize("signature_mode", ["invalid", "missing"])
@pytest.mark.asyncio
async def test_twilio_status_rejects_invalid_or_missing_signature_without_manager_call(
    realtime_api: RealtimeAPIHarness,
    signature_mode: str,
) -> None:
    body, headers = signed_twilio_form(
        TWILIO_STATUS_PATH,
        [
            ("CallSid", TWILIO_CALL_SID),
            ("CallStatus", "completed"),
        ],
    )
    if signature_mode == "invalid":
        headers["X-Twilio-Signature"] = "invalid-signature"
    else:
        del headers["X-Twilio-Signature"]

    response = await realtime_api.client.post(
        TWILIO_STATUS_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Twilio webhook signature is invalid"}
    assert realtime_api.manager.status_calls == []


@pytest.mark.asyncio
async def test_twilio_status_maps_manager_failure_to_generic_conflict(
    realtime_api: RealtimeAPIHarness,
) -> None:
    sensitive_detail = "carrier state leaked auth-token-secret"
    realtime_api.manager.status_error = OpenAIRealtimeError(sensitive_detail)
    body, headers = signed_twilio_form(
        TWILIO_STATUS_PATH,
        [
            ("CallSid", TWILIO_CALL_SID),
            ("CallStatus", "completed"),
        ],
    )

    response = await realtime_api.client.post(
        TWILIO_STATUS_PATH,
        content=body,
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Carrier status could not be reconciled"}
    assert sensitive_detail not in response.text
    assert len(realtime_api.manager.status_calls) == 1


@pytest.mark.asyncio
async def test_health_and_readiness_report_realtime_configuration_and_active_calls(
    realtime_api: RealtimeAPIHarness,
) -> None:
    realtime_api.manager.active_calls = 7

    health = await realtime_api.client.get("/health")
    ready = await realtime_api.client.get("/readyz")

    assert health.status_code == 200
    payload = health.json()
    assert payload["status"] == "ok"
    assert payload["transport"] == "openai_realtime"
    assert payload["openai_realtime_configured"] is True
    assert payload["openai_realtime_runtime_ready"] is True
    assert payload["twilio_configured"] is True
    assert payload["active_realtime_calls"] == 7
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert_security_headers(health)
    assert_security_headers(ready)

    realtime_api.app.state.realtime_manager = None
    missing_manager = await realtime_api.client.get("/readyz")
    assert missing_manager.status_code == 503
    assert missing_manager.json() == {"detail": "Realtime transport is not ready"}

    realtime_api.app.state.realtime_manager = realtime_api.manager
    realtime_api.settings.openai_webhook_secret = SecretStr("")
    missing_configuration = await realtime_api.client.get("/readyz")
    assert missing_configuration.status_code == 503
    assert missing_configuration.json() == {"detail": "Realtime transport is not ready"}


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.asyncio
async def test_realtime_webhook_body_limit_rejects_fixed_and_chunked_requests(
    realtime_api: RealtimeAPIHarness,
    chunked: bool,
) -> None:
    if chunked:

        async def content() -> AsyncIterator[bytes]:
            yield b"a" * 40_000
            yield b"b" * 30_000

        request_content: bytes | AsyncIterator[bytes] = content()
    else:
        request_content = b"x" * (64 * 1024 + 1)

    response = await realtime_api.client.post(
        OPENAI_WEBHOOK_PATH,
        content=request_content,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert len(response.content) < 100
    assert realtime_api.manager.webhook_calls == []
    assert_security_headers(response)
