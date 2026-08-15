from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

import httpx
import pytest
from pydantic import SecretStr

from agent_hotline.telephony_security import verify_correlation_signature
from agent_hotline.vobiz import (
    VobizAPIError,
    VobizCallResult,
    VobizClient,
    build_answer_callback_url,
    build_answer_event_signature,
    build_hangup_callback_url,
    build_hangup_event_signature,
    build_inbound_bridge_xml,
    build_outbound_bridge_xml,
    build_ring_callback_url,
    build_ring_event_signature,
    compute_vobiz_webhook_signature,
    decode_vobiz_correlation_headers,
    decode_vobiz_sip_header_value,
    encode_vobiz_sip_header_value,
    normalize_e164,
    validate_vobiz_auth_id,
    validate_vobiz_call_uuid,
    verify_answer_event_signature,
    verify_hangup_event_signature,
    verify_ring_event_signature,
    verify_vobiz_webhook_headers,
    verify_vobiz_webhook_signature,
)

AUTH_ID = "MA_HOTLINE1234"
AUTH_TOKEN = "vobiz-auth-token-for-tests"
CALL_UUID = "550e8400-e29b-41d4-a716-446655440000"
EVENT_ID = "evt_0123456789abcdef"
PROJECT_ID = "proj_hotline_test"
CORRELATION_SECRET = "sip-correlation-signing-token-for-tests-123456"
NONCE = "12345678901234567890"


def configured_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "vobiz_auth_id": AUTH_ID,
        "vobiz_auth_token": SecretStr(AUTH_TOKEN),
        "vobiz_phone_number": "+12025550100",
        "owner_phone_number": SecretStr("+12025550199"),
        "public_base_url": "https://hotline.example/base",
        "hotline_sip_correlation_secret": SecretStr(CORRELATION_SECRET),
        "hotline_max_call_duration_seconds": 1800,
        "hotline_outbound_ring_timeout_seconds": 30,
        "hotline_retry_attempts": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def parse_vobiz_xml(xml: str) -> tuple[ElementTree.Element, dict[str, str]]:
    root = ElementTree.fromstring(xml)
    user = root.find("./Dial/User")
    assert user is not None
    encoded = user.attrib["sipHeaders"]
    assert re.fullmatch(r"[A-Za-z0-9=,]+", encoded)
    vobiz_headers = {
        f"X-VH-{name}": value
        for item in encoded.split(",")
        for name, value in [item.split("=", 1)]
    }
    return root, decode_vobiz_correlation_headers(vobiz_headers)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+12025550123", "+12025550123"),
        (" +1 (202) 555-0123 ", "+12025550123"),
        ("+91-80000-00001", "+918000000001"),
    ],
)
def test_normalize_e164(raw: str, expected: str) -> None:
    assert normalize_e164(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "12025550123", "+02025550123", "+1234567", "+1234567890123456", "+1\n2025550123"],
)
def test_normalize_e164_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_e164(raw)


@pytest.mark.parametrize("value", [AUTH_ID, "MA_ab12", "MA_" + "A" * 64])
def test_auth_id_validation_accepts_path_safe_master_ids(value: str) -> None:
    assert validate_vobiz_auth_id(value) == value


@pytest.mark.parametrize("value", ["", "SA_HOTLINE1234", "MA_bad-id", "MA_A/Call/evil"])
def test_auth_id_validation_rejects_unsafe_ids(value: str) -> None:
    with pytest.raises(ValueError, match=r"VOBIZ_AUTH_ID|MA_"):
        validate_vobiz_auth_id(value)


def test_call_uuid_validation_is_canonical_and_rejects_nil_or_injection() -> None:
    assert validate_vobiz_call_uuid(CALL_UUID.upper()) == CALL_UUID
    for value in (
        "00000000-0000-0000-0000-000000000000",
        "550e8400e29b41d4a716446655440000",
        CALL_UUID + "/transfer",
        CALL_UUID + "\r\nX-Evil: yes",
    ):
        with pytest.raises(ValueError, match=r"UUID|nil"):
            validate_vobiz_call_uuid(value)


def test_signed_callback_urls_preserve_base_path_and_are_domain_separated() -> None:
    builders = (
        (build_answer_callback_url, build_answer_event_signature, verify_answer_event_signature),
        (build_ring_callback_url, build_ring_event_signature, verify_ring_event_signature),
        (build_hangup_callback_url, build_hangup_event_signature, verify_hangup_event_signature),
    )
    signatures: set[str] = set()
    for url_builder, signature_builder, verifier in builders:
        callback = url_builder(
            public_base_url="https://hotline.example/base/",
            event_id=EVENT_ID,
            correlation_secret=CORRELATION_SECRET,
        )
        parsed = urlsplit(callback)
        query = parse_qs(parsed.query)
        signature = signature_builder(CORRELATION_SECRET, event_id=EVENT_ID)
        assert parsed.path.startswith("/base/v1/vobiz/")
        assert query == {"event_id": [EVENT_ID], "event_sig": [signature]}
        assert verifier(CORRELATION_SECRET, event_id=EVENT_ID, signature=signature)
        assert not verifier(CORRELATION_SECRET, event_id="evt_other", signature=signature)
        assert CORRELATION_SECRET not in callback
        signatures.add(signature)
    assert len(signatures) == 3


@pytest.mark.parametrize(
    "base_url",
    [
        "http://hotline.example",
        "hotline.example",
        "https://user:password@hotline.example",
        "https://hotline.example?query=yes",
        "https://hotline.example/#fragment",
        "https://bad_host.example",
        "https://hotline.example\\callback",
        "https://hotline.example\r\nX-Evil: yes",
    ],
)
def test_callback_urls_reject_unsafe_public_urls(base_url: str) -> None:
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        build_answer_callback_url(
            public_base_url=base_url,
            event_id=EVENT_ID,
            correlation_secret=CORRELATION_SECRET,
        )


def test_vobiz_v2_and_v3_webhook_signatures_match_documented_algorithm() -> None:
    callback_url = "https://hotline.example/base/v1/vobiz/ring?event_id=ignored"
    base_url = "https://hotline.example/base/v1/vobiz/ring"
    for version, message in ((2, base_url + NONCE), (3, base_url + "." + NONCE)):
        expected = base64.b64encode(
            hmac.new(AUTH_TOKEN.encode(), message.encode(), hashlib.sha256).digest()
        ).decode()
        signature = compute_vobiz_webhook_signature(
            callback_url=callback_url,
            nonce=NONCE,
            auth_token=SecretStr(AUTH_TOKEN),
            version=version,
        )
        assert signature == expected
        assert verify_vobiz_webhook_signature(
            callback_url=callback_url,
            nonce=NONCE,
            signature=signature,
            auth_token=AUTH_TOKEN,
            version=version,
        )
        assert not verify_vobiz_webhook_signature(
            callback_url=callback_url,
            nonce="0" * 20,
            signature=signature,
            auth_token=AUTH_TOKEN,
            version=version,
        )


def test_vobiz_v3_headers_are_preferred_without_downgrade_to_v2() -> None:
    callback_url = "https://hotline.example/base/v1/vobiz/hangup?event_id=ignored"
    nonce_v2 = "09876543210987654321"
    signature_v3 = compute_vobiz_webhook_signature(
        callback_url=callback_url,
        nonce=NONCE,
        auth_token=AUTH_TOKEN,
        version=3,
    )
    signature_v2 = compute_vobiz_webhook_signature(
        callback_url=callback_url,
        nonce=nonce_v2,
        auth_token=AUTH_TOKEN,
        version=2,
    )

    verification = verify_vobiz_webhook_headers(
        callback_url=callback_url,
        auth_token=AUTH_TOKEN,
        signature_v3=signature_v3,
        nonce_v3=NONCE,
        signature_v2=signature_v2,
        nonce_v2=nonce_v2,
    )
    assert verification is not None
    assert (verification.version, verification.nonce) == (3, NONCE)

    verification = verify_vobiz_webhook_headers(
        callback_url=callback_url,
        auth_token=AUTH_TOKEN,
        signature_v3=None,
        nonce_v3=None,
        signature_v2=signature_v2,
        nonce_v2=nonce_v2,
    )
    assert verification is not None
    assert (verification.version, verification.nonce) == (2, nonce_v2)

    assert (
        verify_vobiz_webhook_headers(
            callback_url=callback_url,
            auth_token=AUTH_TOKEN,
            signature_v3="invalid",
            nonce_v3=NONCE,
            signature_v2=signature_v2,
            nonce_v2=nonce_v2,
        )
        is None
    )
    assert (
        verify_vobiz_webhook_headers(
            callback_url=callback_url,
            auth_token=AUTH_TOKEN,
            signature_v3=None,
            nonce_v3=NONCE,
            signature_v2=signature_v2,
            nonce_v2=nonce_v2,
        )
        is None
    )


@pytest.mark.parametrize("value", ["outbound", CALL_UUID, EVENT_ID, "+12025550199", "å-safe"])
def test_vobiz_header_encoding_is_alphanumeric_and_round_trips(value: str) -> None:
    encoded = encode_vobiz_sip_header_value(value)
    assert encoded.isalnum()
    assert decode_vobiz_sip_header_value(encoded) == value
    with pytest.raises(ValueError, match="Base32"):
        decode_vobiz_sip_header_value(encoded.lower())


def test_outbound_bridge_xml_uses_user_sipheaders_and_decodes_to_canonical_values() -> None:
    xml = build_outbound_bridge_xml(
        openai_project_id=PROJECT_ID,
        event_id=EVENT_ID,
        correlation_secret=CORRELATION_SECRET,
        correlation_call_uuid=CALL_UUID,
    )
    root, headers = parse_vobiz_xml(xml)
    dial = root.find("./Dial")
    user = root.find("./Dial/User")
    assert root.tag == "Response"
    assert dial is not None and dial.attrib == {"timeLimit": "1800", "timeout": "30"}
    assert user is not None
    assert user.text == f"sip:{PROJECT_ID}@sip.api.openai.com:5061;transport=tls"
    assert root.find("./Hangup") is not None
    assert headers.keys() == {
        "x-hotline-direction",
        "x-hotline-call-sid",
        "x-hotline-event-id",
        "x-hotline-signature",
    }
    assert headers["x-hotline-direction"] == "outbound"
    assert headers["x-hotline-call-sid"] == CALL_UUID
    assert headers["x-hotline-event-id"] == EVENT_ID
    assert verify_correlation_signature(
        CORRELATION_SECRET,
        direction="outbound",
        call_sid=CALL_UUID,
        event_id=EVENT_ID,
        signature=headers["x-hotline-signature"],
    )
    assert CORRELATION_SECRET not in xml


def test_inbound_bridge_xml_round_trips_signed_admission() -> None:
    xml = build_inbound_bridge_xml(
        openai_project_id=PROJECT_ID,
        call_uuid=CALL_UUID,
        caller_phone="+12025550199",
        admission_nonce="admission_nonce_0123456789",
        expires_at_epoch=1_800_000_000,
        correlation_secret=CORRELATION_SECRET,
    )
    _, headers = parse_vobiz_xml(xml)
    assert headers["x-hotline-direction"] == "inbound"
    assert verify_correlation_signature(
        CORRELATION_SECRET,
        direction="inbound",
        call_sid=CALL_UUID,
        event_id=None,
        caller_phone="+12025550199",
        admission_nonce="admission_nonce_0123456789",
        expires_at_epoch=1_800_000_000,
        signature=headers["x-hotline-signature"],
    )


@pytest.mark.asyncio
async def test_place_call_posts_documented_json_and_returns_request_uuid() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"message": "Call fired", "request_uuid": CALL_UUID})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        result = await VobizClient(configured_settings(), client=http_client).place_call(EVENT_ID)

    assert result == VobizCallResult(attempt_id=CALL_UUID, provider="vobiz")
    assert seen["method"] == "POST"
    assert seen["url"] == f"https://api.vobiz.ai/api/v1/Account/{AUTH_ID}/Call/"
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-auth-id"] == AUTH_ID
    assert headers["x-auth-token"] == AUTH_TOKEN
    payload = seen["json"]
    assert isinstance(payload, dict)
    assert payload["from"] == "+12025550100"
    assert payload["to"] == "+12025550199"
    assert payload["answer_method"] == payload["ring_method"] == payload["hangup_method"] == "POST"
    assert payload["time_limit"] == 1800
    assert payload["hangup_on_ring"] == 30
    assert payload["answer_url"].startswith("https://hotline.example/base/v1/vobiz/voice/outbound?")
    assert payload["ring_url"].startswith("https://hotline.example/base/v1/vobiz/ring?")
    assert payload["hangup_url"].startswith("https://hotline.example/base/v1/vobiz/hangup?")
    assert AUTH_TOKEN.encode() not in seen["body"]
    assert CORRELATION_SECRET.encode() not in seen["body"]


@pytest.mark.asyncio
async def test_end_call_deletes_exact_uuid_resource_and_404_is_idempotent() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await VobizClient(configured_settings(), client=http_client).end_call(CALL_UUID.upper())

    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    assert str(requests[0].url) == (
        f"https://api.vobiz.ai/api/v1/Account/{AUTH_ID}/Call/{CALL_UUID}/"
    )
    assert requests[0].content == b""


@pytest.mark.asyncio
async def test_pre_send_connect_failure_retries_without_logging_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("sensitive transport detail", request=request)
        return httpx.Response(200, json={"request_uuid": CALL_UUID})

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    caplog.set_level(logging.WARNING, logger="agent_hotline.vobiz")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VobizClient(
            configured_settings(),
            client=http_client,
            sleep=fake_sleep,
            rng=random.Random(0),
        )
        assert (await client.place_call(EVENT_ID)).attempt_id == CALL_UUID

    assert calls == 2
    assert len(sleeps) == 1 and 0.25 <= sleeps[0] <= 0.30
    for secret in (AUTH_TOKEN, CORRELATION_SECRET, "+12025550199", "sensitive transport detail"):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "failure",
    [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError],
)
@pytest.mark.asyncio
async def test_ambiguous_creation_failure_is_not_retried_and_marks_outcome_unknown(
    failure: type[httpx.RequestError],
) -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise failure("ambiguous", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = VobizClient(configured_settings(hotline_retry_attempts=5), client=http_client)
        with pytest.raises(VobizAPIError, match="unknown delivery") as error:
            await client.place_call(EVENT_ID)

    assert calls == 1
    assert error.value.outcome_unknown
    assert not error.value.retriable


@pytest.mark.parametrize("status", [400, 401, 402, 429, 500, 503])
@pytest.mark.asyncio
async def test_http_creation_errors_are_safe_and_server_errors_are_unknown(status: int) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": AUTH_TOKEN + CORRELATION_SECRET})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(VobizAPIError, match=f"HTTP {status}") as error:
            await VobizClient(configured_settings(), client=http_client).place_call(EVENT_ID)

    assert error.value.status_code == status
    assert error.value.outcome_unknown is (status >= 500)
    assert AUTH_TOKEN not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b'{"message":"Call fired"}',
        b'{"request_uuid":"bad"}',
    ],
)
@pytest.mark.asyncio
async def test_success_response_without_valid_uuid_marks_outcome_unknown(body: bytes) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(VobizAPIError) as error:
            await VobizClient(configured_settings(), client=http_client).place_call(EVENT_ID)
    assert error.value.outcome_unknown
    assert error.value.status_code == 200


@pytest.mark.asyncio
async def test_client_only_closes_the_http_client_it_owns() -> None:
    external = httpx.AsyncClient()
    carrier = VobizClient(configured_settings(), client=external)
    await carrier.close()
    assert not external.is_closed
    await external.aclose()

    owned = VobizClient(configured_settings())
    owned_http = owned._client
    await owned.close()
    assert owned_http.is_closed
