"""Bounded Twilio bidirectional Media Streams protocol adapter."""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from starlette.websockets import WebSocketDisconnect

from .telephony_security import verify_correlation_signature
from .twilio import normalize_e164, validate_twilio_account_sid, validate_twilio_call_sid

_STREAM_SID_RE = re.compile(r"^MZ[0-9a-fA-F]{32}$")
_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_ADMISSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,299}$")
_MARK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_MAX_MESSAGE_BYTES = 256 * 1024
_MAX_AUDIO_BYTES = 64 * 1024


class TwilioMediaProtocolError(RuntimeError):
    """The authenticated Media Streams peer sent an invalid protocol message."""


class TextWebSocket(Protocol):
    async def receive_text(self) -> str: ...

    async def send_text(self, data: str) -> None: ...

    async def close(self, code: int = 1000, reason: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class TwilioMediaStart:
    account_sid: str
    call_sid: str
    stream_sid: str
    event_id: str | None
    direction: Literal["inbound", "outbound"]
    caller_phone: str | None = None
    admission_nonce: str | None = None
    expires_at_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class TwilioMediaEvent:
    kind: Literal["audio", "dtmf", "mark", "stop"]
    value: str | None = None
    cleared: bool = False


class TwilioMediaStream:
    """Validate Twilio messages and emit only bounded, call-bound media events."""

    def __init__(
        self,
        websocket: TextWebSocket,
        *,
        start: TwilioMediaStart,
    ) -> None:
        self.websocket = websocket
        self.start = start
        self._pending_marks: set[str] = set()
        self._cleared_marks: set[str] = set()
        self._closed = False

    @classmethod
    async def initialize(
        cls,
        websocket: TextWebSocket,
        *,
        expected_account_sid: str,
        correlation_secret: str,
    ) -> TwilioMediaStream:
        connected = await _receive_json(websocket)
        if connected != {
            "event": "connected",
            "protocol": "Call",
            "version": "1.0.0",
        }:
            raise TwilioMediaProtocolError("Twilio media handshake is invalid")

        message = await _receive_json(websocket)
        if message.get("event") != "start":
            raise TwilioMediaProtocolError("Twilio media start message is missing")
        start_payload = message.get("start")
        if not isinstance(start_payload, dict):
            raise TwilioMediaProtocolError("Twilio media start payload is invalid")
        try:
            account_sid = validate_twilio_account_sid(start_payload.get("accountSid"))
            call_sid = validate_twilio_call_sid(start_payload.get("callSid"))
        except (TypeError, ValueError) as exc:
            raise TwilioMediaProtocolError("Twilio media call identity is invalid") from exc
        stream_sid = start_payload.get("streamSid")
        if not isinstance(stream_sid, str) or _STREAM_SID_RE.fullmatch(stream_sid) is None:
            raise TwilioMediaProtocolError("Twilio media StreamSid is invalid")
        if message.get("streamSid") != stream_sid:
            raise TwilioMediaProtocolError("Twilio media StreamSid does not match")
        if account_sid != expected_account_sid:
            raise TwilioMediaProtocolError("Twilio media account is not authorized")
        if start_payload.get("tracks") != ["inbound"]:
            raise TwilioMediaProtocolError("Twilio media track configuration is invalid")
        if start_payload.get("mediaFormat") != {
            "encoding": "audio/x-mulaw",
            "sampleRate": 8000,
            "channels": 1,
        }:
            raise TwilioMediaProtocolError("Twilio media audio format is invalid")

        custom = start_payload.get("customParameters")
        if not isinstance(custom, dict):
            raise TwilioMediaProtocolError("Twilio media correlation is missing")
        direction = custom.get("HotlineDirection")
        event_id = custom.get("HotlineEventId")
        signed_call_sid = custom.get("HotlineCallSid")
        signature = custom.get("HotlineSignature")
        if direction not in {"inbound", "outbound"} or signed_call_sid != call_sid:
            raise TwilioMediaProtocolError("Twilio media correlation is invalid")
        caller_phone: str | None = None
        admission_nonce: str | None = None
        expires_at_epoch: int | None = None
        if direction == "outbound":
            if not isinstance(event_id, str) or _EVENT_ID_RE.fullmatch(event_id) is None:
                raise TwilioMediaProtocolError("Twilio media correlation is invalid")
        else:
            if event_id is not None:
                raise TwilioMediaProtocolError("Twilio media correlation is invalid")
            try:
                caller_phone = normalize_e164(custom.get("HotlineCaller"))
                admission_nonce = custom.get("HotlineAdmission")
                raw_expiry = custom.get("HotlineExpires")
                if (
                    not isinstance(admission_nonce, str)
                    or _ADMISSION_RE.fullmatch(admission_nonce) is None
                    or not isinstance(raw_expiry, str)
                    or not raw_expiry.isascii()
                    or not raw_expiry.isdigit()
                ):
                    raise ValueError
                expires_at_epoch = int(raw_expiry)
            except (TypeError, ValueError):
                raise TwilioMediaProtocolError("Twilio media correlation is invalid") from None
        if not isinstance(signature, str) or not verify_correlation_signature(
            correlation_secret,
            direction=direction,
            call_sid=call_sid,
            event_id=event_id,
            caller_phone=caller_phone,
            admission_nonce=admission_nonce,
            expires_at_epoch=expires_at_epoch,
            signature=signature,
        ):
            raise TwilioMediaProtocolError("Twilio media correlation is invalid")
        return cls(
            websocket,
            start=TwilioMediaStart(
                account_sid=account_sid,
                call_sid=call_sid,
                stream_sid=stream_sid,
                event_id=event_id,
                direction=direction,
                caller_phone=caller_phone,
                admission_nonce=admission_nonce,
                expires_at_epoch=expires_at_epoch,
            ),
        )

    async def receive(self) -> TwilioMediaEvent:
        try:
            message = await _receive_json(self.websocket)
        except WebSocketDisconnect:
            return TwilioMediaEvent("stop")
        event = message.get("event")
        stream_sid = message.get("streamSid")
        if event != "stop" and stream_sid != self.start.stream_sid:
            raise TwilioMediaProtocolError("Twilio media event StreamSid does not match")
        if event == "media":
            media = message.get("media")
            if not isinstance(media, dict) or media.get("track") != "inbound":
                raise TwilioMediaProtocolError("Twilio media audio event is invalid")
            payload = _validated_audio_payload(media.get("payload"))
            return TwilioMediaEvent("audio", payload)
        if event == "dtmf":
            dtmf = message.get("dtmf")
            digit = dtmf.get("digit") if isinstance(dtmf, dict) else None
            if (
                not isinstance(dtmf, dict)
                or dtmf.get("track") != "inbound_track"
                or not isinstance(digit, str)
                or len(digit) != 1
                or digit.upper() not in "0123456789*#ABCD"
            ):
                raise TwilioMediaProtocolError("Twilio media DTMF event is invalid")
            return TwilioMediaEvent("dtmf", digit.upper())
        if event == "mark":
            mark = message.get("mark")
            name = mark.get("name") if isinstance(mark, dict) else None
            if not isinstance(name, str) or name not in self._pending_marks:
                raise TwilioMediaProtocolError("Twilio media mark is unknown")
            self._pending_marks.remove(name)
            cleared = name in self._cleared_marks
            self._cleared_marks.discard(name)
            return TwilioMediaEvent("mark", name, cleared=cleared)
        if event == "stop":
            stop = message.get("stop")
            if not isinstance(stop, dict):
                raise TwilioMediaProtocolError("Twilio media stop event is invalid")
            if (
                stop.get("accountSid") != self.start.account_sid
                or stop.get("callSid") != self.start.call_sid
                or message.get("streamSid") != self.start.stream_sid
            ):
                raise TwilioMediaProtocolError("Twilio media stop identity does not match")
            return TwilioMediaEvent("stop")
        raise TwilioMediaProtocolError("Twilio media event type is unsupported")

    async def send_audio(self, payload: str) -> None:
        normalized = _validated_audio_payload(payload)
        await self._send(
            {
                "event": "media",
                "streamSid": self.start.stream_sid,
                "media": {"payload": normalized},
            }
        )

    async def send_mark(self, name: str) -> None:
        if not isinstance(name, str) or _MARK_NAME_RE.fullmatch(name) is None:
            raise TwilioMediaProtocolError("Twilio media mark name is invalid")
        if name in self._pending_marks:
            raise TwilioMediaProtocolError("Twilio media mark name was reused")
        self._pending_marks.add(name)
        await self._send(
            {
                "event": "mark",
                "streamSid": self.start.stream_sid,
                "mark": {"name": name},
            }
        )

    async def clear_audio(self) -> None:
        self._cleared_marks.update(self._pending_marks)
        await self._send(
            {
                "event": "clear",
                "streamSid": self.start.stream_sid,
            }
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self.websocket.close(code=1000, reason="media session ended")

    async def _send(self, message: dict[str, object]) -> None:
        if self._closed:
            raise TwilioMediaProtocolError("Twilio media stream is closed")
        await self.websocket.send_text(json.dumps(message, allow_nan=False, separators=(",", ":")))


async def _receive_json(websocket: TextWebSocket) -> dict[str, object]:
    raw = await websocket.receive_text()
    if len(raw.encode("utf-8")) > _MAX_MESSAGE_BYTES:
        raise TwilioMediaProtocolError("Twilio media message exceeds the size limit")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TwilioMediaProtocolError("Twilio media message is not valid JSON") from exc
    if not isinstance(message, dict):
        raise TwilioMediaProtocolError("Twilio media message is not an object")
    return message


def _validated_audio_payload(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > ((_MAX_AUDIO_BYTES * 4) // 3 + 8):
        raise TwilioMediaProtocolError("Twilio media audio payload is invalid")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TwilioMediaProtocolError("Twilio media audio payload is not Base64") from exc
    if not decoded or len(decoded) > _MAX_AUDIO_BYTES:
        raise TwilioMediaProtocolError("Twilio media audio payload is outside limits")
    return value


__all__ = [
    "TwilioMediaEvent",
    "TwilioMediaProtocolError",
    "TwilioMediaStart",
    "TwilioMediaStream",
]
