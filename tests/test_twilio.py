import base64
import hashlib
import hmac
import logging
import random
from types import SimpleNamespace
from urllib.parse import parse_qs, parse_qsl
from xml.etree import ElementTree

import httpx
import pytest
from pydantic import SecretStr

from agent_hotline.settings import Settings
from agent_hotline.twilio import (
    TwilioAPIError,
    TwilioCallResult,
    TwilioClient,
    build_inbound_bridge_twiml,
    build_outbound_bridge_twiml,
    build_outbound_voice_event_signature,
    build_outbound_voice_url,
    build_status_callback_url,
    build_status_event_signature,
    compute_twilio_webhook_signature,
    normalize_e164,
    verify_correlation_signature,
    verify_outbound_voice_event_signature,
    verify_twilio_webhook_signature,
)

ACCOUNT_SID = "AC" + ("a" * 32)
CALL_SID = "CA" + ("b" * 32)
AUTH_TOKEN = "twilio-auth-token-for-tests"
CALLBACK_TOKEN = "sip-correlation-signing-token-for-tests-123456"
PROJECT_ID = "proj_hotline_test"
EVENT_ID = "evt_0123456789abcdef"
ADMISSION_NONCE = "admission_test_nonce_0123456789abcdef"
ADMISSION_EXPIRES_AT_EPOCH = 1_800_000_000
PUBLIC_BASE_URL = "https://hotline.example"


def configured_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "twilio_account_sid": ACCOUNT_SID,
        "twilio_auth_token": SecretStr(AUTH_TOKEN),
        "twilio_phone_number": "+12025550100",
        "owner_phone_number": SecretStr("+12025550199"),
        "openai_project_id": PROJECT_ID,
        "public_base_url": "https://hotline.example",
        "hotline_sip_correlation_secret": SecretStr(CALLBACK_TOKEN),
        "hotline_max_call_duration_seconds": 1800,
        "hotline_outbound_ring_timeout_seconds": 30,
        "hotline_retry_attempts": 1,
    }
    values.update(overrides)
    return Settings(**values)


def raw_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "twilio_account_sid": ACCOUNT_SID,
        "twilio_auth_token": SecretStr(AUTH_TOKEN),
        "twilio_phone_number": "+12025550100",
        "owner_phone_number": SecretStr("+12025550199"),
        "openai_project_id": PROJECT_ID,
        "public_base_url": "https://hotline.example",
        "hotline_sip_correlation_secret": SecretStr(CALLBACK_TOKEN),
        "hotline_max_call_duration_seconds": 1800,
        "hotline_outbound_ring_timeout_seconds": 30,
        "hotline_retry_attempts": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def parse_bridge_twiml(twiml: str) -> tuple[str, dict[str, str]]:
    root = ElementTree.fromstring(twiml)
    assert root.tag == "Response"
    sip = root.find("./Dial/Sip")
    assert sip is not None
    assert sip.text is not None
    base_uri, separator, query = sip.text.partition("?")
    assert separator == "?"
    headers = dict(parse_qsl(query, keep_blank_values=True, strict_parsing=True))
    return base_uri, headers


def independent_correlation_signature(
    *,
    direction: str,
    call_sid: str,
    event_id: str | None,
    caller_phone: str | None = None,
    admission_nonce: str | None = None,
    expires_at_epoch: int | None = None,
) -> str:
    payload = (
        f"v3\n{direction}\n{call_sid}\n{event_id or ''}\n{caller_phone or ''}\n"
        f"{admission_nonce or ''}\n{expires_at_epoch or ''}"
    ).encode()
    digest = hmac.new(CALLBACK_TOKEN.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+12025550123", "+12025550123"),
        (" +1 (202) 555-0123 ", "+12025550123"),
        ("+44 20 7946 0958", "+442079460958"),
        ("+91-80000-00001", "+918000000001"),
    ],
)
def test_normalize_e164_accepts_safe_visual_formatting(raw: str, expected: str) -> None:
    assert normalize_e164(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "12025550123",
        "0012025550123",
        "+02025550123",
        "+1234567",
        "+1234567890123456",
        "+1-202-555-0123 ext 4",
        "+١٢٠٢٥٥٥٠١٢٣",
        "+1202\n5550123",
        "+12025550123\n",
    ],
)
def test_normalize_e164_rejects_non_e164_input(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_e164(raw)


def test_normalize_e164_rejects_non_string_input() -> None:
    with pytest.raises(TypeError, match="string"):
        normalize_e164(12025550123)  # type: ignore[arg-type]


def test_outbound_twiml_has_signed_event_headers_and_escaped_xml() -> None:
    twiml = build_outbound_bridge_twiml(
        openai_project_id=PROJECT_ID,
        event_id=EVENT_ID,
        correlation_secret=CALLBACK_TOKEN,
        correlation_call_sid=CALL_SID,
    )
    base_uri, headers = parse_bridge_twiml(twiml)
    dial = ElementTree.fromstring(twiml).find("./Dial")

    assert twiml.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert dial is not None
    assert dial.attrib == {"timeLimit": "1800", "timeout": "30"}
    assert base_uri == f"sip:{PROJECT_ID}@sip.api.openai.com;transport=tls"
    assert headers == {
        "X-Hotline-Direction": "outbound",
        "X-Hotline-Call-Sid": CALL_SID,
        "X-Hotline-Event-Id": EVENT_ID,
        "X-Hotline-Signature": independent_correlation_signature(
            direction="outbound",
            call_sid=CALL_SID,
            event_id=EVENT_ID,
        ),
    }
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=CALL_SID,
        event_id=EVENT_ID,
        signature=headers["X-Hotline-Signature"],
    )
    assert "&amp;" in twiml
    assert "&X-Hotline" not in twiml
    assert CALLBACK_TOKEN not in twiml


def test_outbound_twiml_signs_the_allocated_parent_call_sid() -> None:
    another_call_sid = "CA" + ("c" * 32)
    twiml = build_outbound_bridge_twiml(
        openai_project_id=PROJECT_ID,
        event_id=EVENT_ID,
        correlation_secret=CALLBACK_TOKEN,
        correlation_call_sid=another_call_sid,
    )
    _, headers = parse_bridge_twiml(twiml)

    assert headers["X-Hotline-Call-Sid"] == another_call_sid
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="outbound",
        call_sid=another_call_sid,
        event_id=EVENT_ID,
        signature=headers["X-Hotline-Signature"],
    )


def test_inbound_twiml_signs_the_actual_twilio_call_sid() -> None:
    twiml = build_inbound_bridge_twiml(
        openai_project_id=PROJECT_ID,
        call_sid=CALL_SID,
        caller_phone="+12025550199",
        admission_nonce=ADMISSION_NONCE,
        expires_at_epoch=ADMISSION_EXPIRES_AT_EPOCH,
        public_base_url=PUBLIC_BASE_URL,
        correlation_secret=SecretStr(CALLBACK_TOKEN),
    )
    base_uri, headers = parse_bridge_twiml(twiml)
    dial = ElementTree.fromstring(twiml).find("./Dial")

    assert base_uri == f"sip:{PROJECT_ID}@sip.api.openai.com;transport=tls"
    assert dial is not None
    assert dial.attrib == {
        "timeLimit": "1800",
        "timeout": "30",
        "action": f"{PUBLIC_BASE_URL}/v1/twilio/status",
        "method": "POST",
    }
    assert headers == {
        "X-Hotline-Direction": "inbound",
        "X-Hotline-Call-Sid": CALL_SID,
        "X-Hotline-Caller": "+12025550199",
        "X-Hotline-Admission": ADMISSION_NONCE,
        "X-Hotline-Expires": str(ADMISSION_EXPIRES_AT_EPOCH),
        "X-Hotline-Signature": independent_correlation_signature(
            direction="inbound",
            call_sid=CALL_SID,
            event_id=None,
            caller_phone="+12025550199",
            admission_nonce=ADMISSION_NONCE,
            expires_at_epoch=ADMISSION_EXPIRES_AT_EPOCH,
        ),
    }
    assert verify_correlation_signature(
        CALLBACK_TOKEN,
        direction="inbound",
        call_sid=CALL_SID,
        event_id=None,
        caller_phone="+12025550199",
        admission_nonce=ADMISSION_NONCE,
        expires_at_epoch=ADMISSION_EXPIRES_AT_EPOCH,
        signature=headers["X-Hotline-Signature"],
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "openai_project_id": "proj_bad?<Say>owned</Say>",
                "event_id": EVENT_ID,
                "correlation_secret": CALLBACK_TOKEN,
                "correlation_call_sid": CALL_SID,
            },
            "OPENAI_PROJECT_ID",
        ),
        (
            {
                "openai_project_id": PROJECT_ID,
                "event_id": "evt_good&X-Evil=<Say>owned</Say>",
                "correlation_secret": CALLBACK_TOKEN,
                "correlation_call_sid": CALL_SID,
            },
            "event_id",
        ),
        (
            {
                "openai_project_id": PROJECT_ID,
                "event_id": EVENT_ID,
                "correlation_secret": CALLBACK_TOKEN,
                "correlation_call_sid": CALL_SID + "\r\nX-Evil: yes",
            },
            "CallSid",
        ),
        (
            {
                "openai_project_id": PROJECT_ID,
                "event_id": EVENT_ID,
                "correlation_secret": CALLBACK_TOKEN,
                "correlation_call_sid": "",
            },
            "CallSid",
        ),
    ],
)
def test_outbound_twiml_rejects_sip_or_xml_injection(
    kwargs: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_outbound_bridge_twiml(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "call_sid",
    [
        "",
        "CB" + ("a" * 32),
        "CAshort",
        "CA" + ("z" * 32),
        CALL_SID + "\r\nX-Evil: yes",
    ],
)
def test_inbound_twiml_rejects_invalid_twilio_call_sid(call_sid: str) -> None:
    with pytest.raises(ValueError, match="CallSid"):
        build_inbound_bridge_twiml(
            openai_project_id=PROJECT_ID,
            call_sid=call_sid,
            caller_phone="+12025550199",
            admission_nonce=ADMISSION_NONCE,
            expires_at_epoch=ADMISSION_EXPIRES_AT_EPOCH,
            public_base_url=PUBLIC_BASE_URL,
            correlation_secret=CALLBACK_TOKEN,
        )


def test_status_callback_url_preserves_base_path_without_embedding_secrets() -> None:
    callback_url = build_status_callback_url(
        public_base_url="https://hotline.example/base/",
    )

    assert callback_url == "https://hotline.example/base/v1/twilio/status"
    assert CALLBACK_TOKEN not in callback_url


def test_outbound_voice_url_is_event_bound_and_domain_separated() -> None:
    voice_url = build_outbound_voice_url(
        public_base_url="https://hotline.example/base/",
        event_id=EVENT_ID,
        correlation_secret=CALLBACK_TOKEN,
    )
    voice_signature = build_outbound_voice_event_signature(
        CALLBACK_TOKEN,
        event_id=EVENT_ID,
    )

    assert voice_url == (
        "https://hotline.example/base/v1/twilio/voice/outbound"
        f"?event_id={EVENT_ID}&event_sig={voice_signature}"
    )
    assert verify_outbound_voice_event_signature(
        CALLBACK_TOKEN,
        event_id=EVENT_ID,
        signature=voice_signature,
    )
    assert voice_signature != build_status_event_signature(
        CALLBACK_TOKEN,
        event_id=EVENT_ID,
    )
    assert CALLBACK_TOKEN not in voice_url


@pytest.mark.parametrize(
    "public_base_url",
    [
        "http://hotline.example",
        "hotline.example",
        "https://user:password@hotline.example",
        "https://hotline.example?mode=test",
        "https://hotline.example/#fragment",
        "https://bad_host.example",
        "https://-bad.example",
        "https://hotline.example:",
        "https://hotline.example/a raw space",
        "https://hotline.example\\callback",
        "https://hotline.example\r\nX-Evil: yes",
    ],
)
def test_status_callback_url_rejects_unsafe_public_urls(public_base_url: str) -> None:
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        build_status_callback_url(
            public_base_url=public_base_url,
        )


def test_twilio_webhook_signature_matches_official_hmac_sha1_vector() -> None:
    url = "https://example.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675310",
        "Digits": "1234",
        "From": "+14158675310",
        "To": "+18005551212",
    }

    signature = compute_twilio_webhook_signature(
        url=url,
        params=params,
        auth_token="12345",
    )

    assert signature == "L/OH5YylLD5NRKLltdqwSvS0BnU="
    assert verify_twilio_webhook_signature(
        url=url,
        params=params,
        signature=signature,
        auth_token=SecretStr("12345"),
    )


def test_twilio_webhook_signature_sorts_and_deduplicates_multi_values() -> None:
    url = "https://hotline.example/webhook"
    params = [
        ("z", "2"),
        ("A", "b"),
        ("A", "a"),
        ("A", "a"),
    ]
    canonical = f"{url}AaAbz2"
    expected = base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), canonical.encode(), hashlib.sha1).digest()
    ).decode()

    assert (
        compute_twilio_webhook_signature(
            url=url,
            params=params,
            auth_token=AUTH_TOKEN,
        )
        == expected
    )
    assert (
        compute_twilio_webhook_signature(
            url=url,
            params={"z": "2", "A": ("b", "a", "a")},
            auth_token=AUTH_TOKEN,
        )
        == expected
    )


def test_twilio_webhook_signature_supports_an_empty_form_body() -> None:
    url = "https://hotline.example/webhook?fixed=encoded%20value"
    expected = base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), url.encode(), hashlib.sha1).digest()
    ).decode()

    assert (
        compute_twilio_webhook_signature(
            url=url,
            params=None,
            auth_token=AUTH_TOKEN,
        )
        == expected
    )


def test_twilio_webhook_signature_supports_framework_multidicts() -> None:
    class MultiDict(dict[str, str]):
        def getall(self, name: str) -> list[str]:
            return {
                "Alpha": ["two", "one"],
                "Beta": ["three"],
            }[name]

    class QueryDict(dict[str, str]):
        def getlist(self, name: str) -> list[str]:
            return {
                "Alpha": ["two", "one"],
                "Beta": ["three"],
            }[name]

    url = "https://hotline.example/webhook"
    canonical = f"{url}AlphaoneAlphatwoBetathree"
    expected = base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), canonical.encode(), hashlib.sha1).digest()
    ).decode()

    for params in (
        MultiDict(Alpha="ignored", Beta="ignored"),
        QueryDict(Alpha="ignored", Beta="ignored"),
    ):
        assert (
            compute_twilio_webhook_signature(
                url=url,
                params=params,
                auth_token=AUTH_TOKEN,
            )
            == expected
        )


def test_twilio_webhook_verification_binds_exact_url_and_all_form_values() -> None:
    url = "https://hotline.example/webhook?message=hello%20world&type=test%2Bvalue"
    params = {"CallSid": CALL_SID, "Whitespace": " value "}
    signature = compute_twilio_webhook_signature(
        url=url,
        params=params,
        auth_token=AUTH_TOKEN,
    )

    assert verify_twilio_webhook_signature(
        url=url,
        params=params,
        signature=signature,
        auth_token=AUTH_TOKEN,
    )
    assert not verify_twilio_webhook_signature(
        url=url.replace("%20", " "),
        params=params,
        signature=signature,
        auth_token=AUTH_TOKEN,
    )
    assert not verify_twilio_webhook_signature(
        url=url,
        params={**params, "Whitespace": "value"},
        signature=signature,
        auth_token=AUTH_TOKEN,
    )
    assert not verify_twilio_webhook_signature(
        url=url,
        params=params,
        signature=signature[:-1] + "x",
        auth_token=AUTH_TOKEN,
    )


@pytest.mark.parametrize(
    ("url", "params", "signature", "auth_token"),
    [
        ("", {}, "signature", AUTH_TOKEN),
        ("https://hotline.example/#fragment", {}, "signature", AUTH_TOKEN),
        ("https://user:password@hotline.example/", {}, "signature", AUTH_TOKEN),
        ("https://bad_host.example/", {}, "signature", AUTH_TOKEN),
        ("https://hotline.example", {}, None, AUTH_TOKEN),
        ("https://hotline.example", {"A": 1}, "signature", AUTH_TOKEN),
        ("https://hotline.example", {}, "signature", ""),
    ],
)
def test_twilio_webhook_verification_fails_closed_on_bad_inputs(
    url: str,
    params: object,
    signature: str | None,
    auth_token: str,
) -> None:
    assert not verify_twilio_webhook_signature(
        url=url,
        params=params,  # type: ignore[arg-type]
        signature=signature,
        auth_token=auth_token,
    )


@pytest.mark.asyncio
async def test_place_call_posts_exact_twilio_form_and_returns_compatible_result() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["accept"] = request.headers["Accept"]
        seen["content_type"] = request.headers["Content-Type"]
        seen["content"] = request.content
        seen["form"] = parse_qs(request.content.decode(), keep_blank_values=True)
        return httpx.Response(201, json={"sid": CALL_SID, "status": "queued"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(configured_settings(), client=http_client)
        result = await client.place_call(EVENT_ID, object())
        await client.close()
        assert not http_client.is_closed

    assert result == TwilioCallResult(attempt_id=CALL_SID, provider="twilio")
    assert seen["method"] == "POST"
    assert seen["url"] == (f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls.json")
    basic_credentials = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    assert seen["authorization"] == f"Basic {basic_credentials}"
    assert seen["accept"] == "application/json"
    assert str(seen["content_type"]).startswith("application/x-www-form-urlencoded")
    assert CALLBACK_TOKEN.encode() not in seen["content"]

    form = seen["form"]
    assert isinstance(form, dict)
    assert form["To"] == ["+12025550199"]
    assert form["From"] == ["+12025550100"]
    assert form["Url"] == [
        build_outbound_voice_url(
            public_base_url="https://hotline.example",
            event_id=EVENT_ID,
            correlation_secret=CALLBACK_TOKEN,
        )
    ]
    assert form["Method"] == ["POST"]
    assert "Twiml" not in form
    assert form["StatusCallback"] == [
        build_status_callback_url(
            public_base_url="https://hotline.example",
            event_id=EVENT_ID,
            correlation_secret=CALLBACK_TOKEN,
        )
    ]
    assert CALLBACK_TOKEN not in form["StatusCallback"][0]
    assert form["StatusCallbackMethod"] == ["POST"]
    assert form["StatusCallbackEvent"] == [
        "initiated",
        "ringing",
        "answered",
        "completed",
    ]
    assert form["TimeLimit"] == ["1800"]
    assert form["Timeout"] == ["30"]


@pytest.mark.asyncio
async def test_end_call_posts_completed_to_the_exact_twilio_call_resource() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers["Authorization"]
        seen["accept"] = request.headers["Accept"]
        seen["form"] = parse_qs(request.content.decode(), keep_blank_values=True)
        return httpx.Response(200, json={"sid": CALL_SID, "status": "completed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(configured_settings(), client=http_client)
        await client.end_call(CALL_SID)

    assert seen["method"] == "POST"
    assert seen["url"] == (
        f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Calls/{CALL_SID}.json"
    )
    basic_credentials = base64.b64encode(f"{ACCOUNT_SID}:{AUTH_TOKEN}".encode()).decode()
    assert seen["authorization"] == f"Basic {basic_credentials}"
    assert seen["accept"] == "application/json"
    assert seen["form"] == {"Status": ["completed"]}


@pytest.mark.asyncio
async def test_connect_failure_retries_once_without_logging_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("sensitive transport detail", request=request)
        return httpx.Response(201, json={"sid": CALL_SID})

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    caplog.set_level(logging.WARNING, logger="agent_hotline.twilio")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(
            configured_settings(),
            client=http_client,
            sleep=fake_sleep,
            rng=random.Random(0),
        )
        result = await client.place_call(EVENT_ID)

    assert result.attempt_id == CALL_SID
    assert calls == 2
    assert len(sleeps) == 1
    assert 0.25 <= sleeps[0] <= 0.30
    assert "pre-send connection failure" in caplog.text
    for secret in (AUTH_TOKEN, CALLBACK_TOKEN, "+12025550199", "sensitive transport detail"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_exhausted_connect_failures_remain_safely_retriable() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("connect timeout", request=request)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(
            configured_settings(),
            client=http_client,
            sleep=fake_sleep,
        )
        with pytest.raises(TwilioAPIError, match="could not connect") as error:
            await client.place_call(EVENT_ID)

    assert calls == 2
    assert len(sleeps) == 1
    assert error.value.retriable
    assert error.value.status_code is None


@pytest.mark.asyncio
async def test_pool_timeout_is_safe_to_retry_before_request_transmission() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.PoolTimeout("pool timeout", request=request)
        return httpx.Response(201, json={"sid": CALL_SID})

    async def fake_sleep(_: float) -> None:
        return None

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(
            configured_settings(),
            client=http_client,
            sleep=fake_sleep,
        )
        result = await client.place_call(EVENT_ID)

    assert calls == 2
    assert result.attempt_id == CALL_SID


@pytest.mark.parametrize("failure_kind", ["read_timeout", "write_error", "protocol_error"])
@pytest.mark.asyncio
async def test_ambiguous_transport_failures_are_never_retried(
    failure_kind: str,
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if failure_kind == "read_timeout":
            raise httpx.ReadTimeout("read timeout", request=request)
        if failure_kind == "write_error":
            raise httpx.WriteError("write error", request=request)
        raise httpx.RemoteProtocolError("protocol error", request=request)

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(
            configured_settings(hotline_retry_attempts=5),
            client=http_client,
            sleep=fake_sleep,
        )
        with pytest.raises(TwilioAPIError, match="unknown delivery outcome") as error:
            await client.place_call(EVENT_ID)

    assert calls == 1
    assert sleeps == []
    assert not error.value.retriable


@pytest.mark.parametrize("status_code", [302, 408, 429, 500, 503])
@pytest.mark.asyncio
async def test_http_error_responses_are_not_retried_or_echoed(status_code: int) -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status_code,
            json={
                "message": f"bad {AUTH_TOKEN} {CALLBACK_TOKEN} +12025550199",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(
            configured_settings(hotline_retry_attempts=5),
            client=http_client,
        )
        with pytest.raises(TwilioAPIError, match=f"HTTP {status_code}") as error:
            await client.place_call(EVENT_ID)

    assert calls == 1
    assert error.value.status_code == status_code
    assert not error.value.retriable
    assert AUTH_TOKEN not in str(error.value)
    assert CALLBACK_TOKEN not in str(error.value)
    assert "+12025550199" not in str(error.value)


@pytest.mark.parametrize(
    ("response_kind", "message"),
    [
        ("invalid_json", "invalid JSON"),
        ("list", "non-object"),
        ("missing_sid", "valid CallSid"),
        ("invalid_sid", "valid CallSid"),
    ],
)
@pytest.mark.asyncio
async def test_success_response_requires_a_valid_twilio_call_sid(
    response_kind: str,
    message: str,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        if response_kind == "invalid_json":
            return httpx.Response(201, content=b"<html>not json</html>")
        if response_kind == "list":
            return httpx.Response(201, json=[{"sid": CALL_SID}])
        if response_kind == "missing_sid":
            return httpx.Response(201, json={"status": "queued"})
        return httpx.Response(201, json={"sid": "CA-not-a-valid-sid"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TwilioClient(configured_settings(), client=http_client)
        with pytest.raises(TwilioAPIError, match=message) as error:
            await client.place_call(EVENT_ID)

    assert error.value.status_code == 201
    assert not error.value.retriable


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"twilio_account_sid": "ACshort"}, "TWILIO_ACCOUNT_SID"),
        ({"twilio_auth_token": SecretStr("")}, "TWILIO_AUTH_TOKEN"),
        ({"twilio_auth_token": SecretStr("short")}, "TWILIO_AUTH_TOKEN"),
        ({"twilio_phone_number": "2025550100"}, "E.164"),
        ({"owner_phone_number": SecretStr("+02025550199")}, "E.164"),
        ({"openai_project_id": "proj_bad?header=yes"}, "OPENAI_PROJECT_ID"),
        ({"public_base_url": "http://hotline.example"}, "PUBLIC_BASE_URL"),
        (
            {"hotline_sip_correlation_secret": SecretStr("short")},
            "HOTLINE_SIP_CORRELATION_SECRET",
        ),
        ({"hotline_max_call_duration_seconds": 59}, "HOTLINE_MAX_CALL_DURATION_SECONDS"),
        ({"hotline_outbound_ring_timeout_seconds": 0}, "HOTLINE_OUTBOUND_RING_TIMEOUT_SECONDS"),
        ({"hotline_retry_attempts": 6}, "HOTLINE_RETRY_ATTEMPTS"),
    ],
)
def test_client_fails_fast_on_invalid_configuration(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TwilioClient(raw_settings(**overrides))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_client_only_closes_the_http_client_it_owns() -> None:
    external = httpx.AsyncClient()
    carrier = TwilioClient(configured_settings(), client=external)
    await carrier.close()
    assert not external.is_closed
    await external.aclose()

    owned_carrier = TwilioClient(configured_settings())
    owned_http_client = owned_carrier._client
    assert not owned_http_client.is_closed
    await owned_carrier.close()
    assert owned_http_client.is_closed
    await owned_carrier.close()
