from __future__ import annotations

import base64
import hashlib
import hmac
import json
from xml.etree import ElementTree

import pytest

from agent_hotline.telephony_security import (
    build_correlation_signature,
    verify_correlation_signature,
)
from agent_hotline.twilio import (
    build_inbound_media_stream_twiml,
    build_outbound_media_stream_twiml,
    compute_twilio_websocket_signature,
    verify_twilio_websocket_signature,
)
from agent_hotline.twilio_media import TwilioMediaProtocolError, TwilioMediaStream

PUBLIC_BASE_URL = "https://hotline.example.test/base"
EVENT_ID = "evt_twilio_media_test"
ACCOUNT_SID = "AC" + ("a" * 32)
CALL_SID = "CA" + ("b" * 32)
STREAM_SID = "MZ" + ("c" * 32)
CALLER_PHONE = "+12025550123"
ADMISSION_NONCE = "adm_twilio_media_test"
EXPIRES_AT_EPOCH = 1_900_000_000
AUTH_TOKEN = "twilio-media-auth-token-for-tests"
CORRELATION_SECRET = "twilio-media-correlation-secret-for-tests-123456"


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict[str, object]] = []
        self.closed: list[tuple[int, str | None]] = []

    async def receive_text(self) -> str:
        return self.messages.pop(0)

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed.append((code, reason))


def start_messages(
    *,
    direction: str = "outbound",
    signature: str | None = None,
) -> list[dict[str, object]]:
    correlation: dict[str, object] = {
        "HotlineDirection": direction,
        "HotlineCallSid": CALL_SID,
    }
    if direction == "outbound":
        correlation["HotlineEventId"] = EVENT_ID
        correlation["HotlineSignature"] = signature or build_correlation_signature(
            CORRELATION_SECRET,
            direction="outbound",
            call_sid=CALL_SID,
            event_id=EVENT_ID,
        )
    else:
        correlation.update(
            {
                "HotlineCaller": CALLER_PHONE,
                "HotlineAdmission": ADMISSION_NONCE,
                "HotlineExpires": str(EXPIRES_AT_EPOCH),
                "HotlineSignature": signature
                or build_correlation_signature(
                    CORRELATION_SECRET,
                    direction="inbound",
                    call_sid=CALL_SID,
                    caller_phone=CALLER_PHONE,
                    admission_nonce=ADMISSION_NONCE,
                    expires_at_epoch=EXPIRES_AT_EPOCH,
                ),
            }
        )
    return [
        {"event": "connected", "protocol": "Call", "version": "1.0.0"},
        {
            "event": "start",
            "sequenceNumber": "1",
            "streamSid": STREAM_SID,
            "start": {
                "accountSid": ACCOUNT_SID,
                "callSid": CALL_SID,
                "streamSid": STREAM_SID,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": 8000,
                    "channels": 1,
                },
                "customParameters": correlation,
            },
        },
    ]


@pytest.mark.parametrize(
    ("direction", "builder"),
    [
        ("outbound", build_outbound_media_stream_twiml),
        ("inbound", build_inbound_media_stream_twiml),
    ],
)
def test_media_stream_twiml_is_call_bound_and_contains_no_secret(
    direction: str,
    builder: object,
) -> None:
    if direction == "outbound":
        twiml = builder(  # type: ignore[operator]
            public_base_url=PUBLIC_BASE_URL,
            event_id=EVENT_ID,
            correlation_secret=CORRELATION_SECRET,
            correlation_call_sid=CALL_SID,
        )
    else:
        twiml = builder(  # type: ignore[operator]
            public_base_url=PUBLIC_BASE_URL,
            call_sid=CALL_SID,
            caller_phone=CALLER_PHONE,
            admission_nonce=ADMISSION_NONCE,
            expires_at_epoch=EXPIRES_AT_EPOCH,
            correlation_secret=CORRELATION_SECRET,
        )
    root = ElementTree.fromstring(twiml)
    stream = root.find("./Connect/Stream")
    assert stream is not None
    assert stream.attrib == {"url": "wss://hotline.example.test/base/v1/twilio/media"}
    parameters = {
        item.attrib["name"]: item.attrib["value"] for item in stream.findall("./Parameter")
    }
    assert parameters["HotlineDirection"] == direction
    assert parameters["HotlineCallSid"] == CALL_SID
    if direction == "outbound":
        assert parameters["HotlineEventId"] == EVENT_ID
        correlation_fields = {"event_id": EVENT_ID}
    else:
        assert "HotlineEventId" not in parameters
        assert parameters["HotlineCaller"] == CALLER_PHONE
        assert parameters["HotlineAdmission"] == ADMISSION_NONCE
        assert parameters["HotlineExpires"] == str(EXPIRES_AT_EPOCH)
        correlation_fields = {
            "event_id": None,
            "caller_phone": CALLER_PHONE,
            "admission_nonce": ADMISSION_NONCE,
            "expires_at_epoch": EXPIRES_AT_EPOCH,
        }
    assert verify_correlation_signature(
        CORRELATION_SECRET,
        direction=direction,
        call_sid=CALL_SID,
        **correlation_fields,  # type: ignore[arg-type]
        signature=parameters["HotlineSignature"],
    )
    assert CORRELATION_SECRET not in twiml


def test_websocket_signature_matches_twilio_hmac_shape() -> None:
    url = "wss://hotline.example.test/v1/twilio/media"
    expected = base64.b64encode(
        hmac.new(AUTH_TOKEN.encode(), url.encode(), hashlib.sha1).digest()
    ).decode()
    signature = compute_twilio_websocket_signature(
        url=url,
        params=None,
        auth_token=AUTH_TOKEN,
    )
    assert signature == expected
    assert verify_twilio_websocket_signature(
        url=url,
        params=None,
        signature=signature,
        auth_token=AUTH_TOKEN,
    )
    assert not verify_twilio_websocket_signature(
        url=url,
        params=None,
        signature="not-valid",
        auth_token=AUTH_TOKEN,
    )


@pytest.mark.asyncio
async def test_media_stream_validates_and_bridges_audio_marks_and_dtmf() -> None:
    audio = base64.b64encode(bytes(range(160))).decode()
    websocket = FakeWebSocket(
        [
            *start_messages(),
            {
                "event": "media",
                "sequenceNumber": "2",
                "streamSid": STREAM_SID,
                "media": {"track": "inbound", "payload": audio},
            },
            {
                "event": "dtmf",
                "sequenceNumber": "3",
                "streamSid": STREAM_SID,
                "dtmf": {"track": "inbound_track", "digit": "7"},
            },
        ]
    )
    stream = await TwilioMediaStream.initialize(
        websocket,
        expected_account_sid=ACCOUNT_SID,
        correlation_secret=CORRELATION_SECRET,
    )
    assert stream.start.event_id == EVENT_ID
    assert stream.start.direction == "outbound"
    assert (await stream.receive()).value == audio
    assert (await stream.receive()).value == "7"

    await stream.send_audio(audio)
    await stream.send_mark("resp_test")
    await stream.clear_audio()
    websocket.messages.append(
        json.dumps(
            {
                "event": "mark",
                "sequenceNumber": "4",
                "streamSid": STREAM_SID,
                "mark": {"name": "resp_test"},
            }
        )
    )
    mark = await stream.receive()
    assert mark.kind == "mark"
    assert mark.cleared is True
    assert websocket.sent == [
        {"event": "media", "streamSid": STREAM_SID, "media": {"payload": audio}},
        {"event": "mark", "streamSid": STREAM_SID, "mark": {"name": "resp_test"}},
        {"event": "clear", "streamSid": STREAM_SID},
    ]


@pytest.mark.asyncio
async def test_media_stream_rejects_forged_correlation() -> None:
    websocket = FakeWebSocket(start_messages(signature="forged"))
    with pytest.raises(TwilioMediaProtocolError, match="correlation"):
        await TwilioMediaStream.initialize(
            websocket,
            expected_account_sid=ACCOUNT_SID,
            correlation_secret=CORRELATION_SECRET,
        )


@pytest.mark.asyncio
async def test_inbound_media_stream_validates_expiring_carrier_admission() -> None:
    stream = await TwilioMediaStream.initialize(
        FakeWebSocket(start_messages(direction="inbound")),
        expected_account_sid=ACCOUNT_SID,
        correlation_secret=CORRELATION_SECRET,
    )

    assert stream.start.direction == "inbound"
    assert stream.start.event_id is None
    assert stream.start.caller_phone == CALLER_PHONE
    assert stream.start.admission_nonce == ADMISSION_NONCE
    assert stream.start.expires_at_epoch == EXPIRES_AT_EPOCH
