"""Vobiz carrier transport and safe Voice XML helpers.

Vobiz owns only the PSTN/SIP carrier leg. OpenAI Realtime remains the
conversation runtime, and the daemon remains the authority boundary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import ipaddress
import logging
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit
from uuid import UUID
from xml.etree import ElementTree

import httpx

from .telephony_security import build_correlation_signature

if TYPE_CHECKING:
    from .settings import Settings

logger = logging.getLogger(__name__)

VOBIZ_API_BASE_URL = "https://api.vobiz.ai/api/v1"
VOBIZ_OUTBOUND_VOICE_PATH = "/v1/vobiz/voice/outbound"
VOBIZ_INBOUND_VOICE_PATH = "/v1/vobiz/voice/incoming"
VOBIZ_RING_CALLBACK_PATH = "/v1/vobiz/ring"
VOBIZ_HANGUP_CALLBACK_PATH = "/v1/vobiz/hangup"
# Compatibility alias for callers that model hangup as the terminal status callback.
VOBIZ_STATUS_CALLBACK_PATH = VOBIZ_HANGUP_CALLBACK_PATH
OPENAI_SIP_HOST = "sip.api.openai.com"
OPENAI_SIP_PORT = 5061

_AUTH_ID_PATTERN = re.compile(r"^MA_[A-Za-z0-9]{4,64}$")
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_EVENT_ID_PATTERN = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_PROJECT_ID_PATTERN = re.compile(r"^proj_[A-Za-z0-9_-]{1,128}$")
_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
_HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_VOBIZ_NONCE_PATTERN = re.compile(r"^[0-9]{20}$")
_VOBIZ_ENCODED_VALUE_PATTERN = re.compile(r"^[A-Z2-7]+$")
_PHONE_SEPARATORS = frozenset(" .()-")
_SAFE_PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_MAX_CALLBACK_URL_BYTES = 2048
_MAX_SIP_HEADERS_BYTES = 1024
_MAX_ENCODED_HEADER_VALUE_BYTES = 512

_CANONICAL_TO_VOBIZ_CORRELATION_HEADERS = {
    "x-hotline-direction": "HotlineDirection",
    "x-hotline-call-sid": "HotlineCallSid",
    "x-hotline-event-id": "HotlineEventId",
    "x-hotline-caller": "HotlineCaller",
    "x-hotline-admission": "HotlineAdmission",
    "x-hotline-expires": "HotlineExpires",
    "x-hotline-signature": "HotlineSignature",
}
_VOBIZ_TO_CANONICAL_CORRELATION_HEADERS = {
    f"x-vh-{vobiz.lower()}": canonical
    for canonical, vobiz in _CANONICAL_TO_VOBIZ_CORRELATION_HEADERS.items()
}


class _SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


SecretInput = str | _SecretValue


class VobizAPIError(RuntimeError):
    """A Vobiz failure whose message is safe to expose in logs."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retriable: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class VobizCallResult:
    """The carrier result, structurally compatible with ``CallAttempt``."""

    attempt_id: str
    provider: Literal["vobiz"] = "vobiz"


@dataclass(frozen=True, slots=True)
class VobizWebhookVerification:
    """Verified signature metadata for the API's durable nonce claim."""

    version: Literal[2, 3]
    nonce: str


def normalize_e164(value: str) -> str:
    """Normalize safe visual separators and return a strict E.164 number."""

    if not isinstance(value, str):
        raise TypeError("phone number must be a string")
    _reject_control_characters(value, "phone number")
    normalized = "".join(
        character for character in value.strip() if character not in _PHONE_SEPARATORS
    )
    if _E164_PATTERN.fullmatch(normalized) is None:
        raise ValueError("phone number must be E.164, for example +12025550123")
    return normalized


def validate_vobiz_auth_id(value: str | None) -> str:
    """Return one path-safe Vobiz master-account Auth ID."""

    auth_id = _require_text(value, "VOBIZ_AUTH_ID")
    if _AUTH_ID_PATTERN.fullmatch(auth_id) is None:
        raise ValueError("VOBIZ_AUTH_ID must be an MA_-prefixed account ID")
    return auth_id


def validate_vobiz_call_uuid(value: str | None) -> str:
    """Validate a Vobiz call ID and return its canonical lowercase UUID."""

    call_uuid = _require_text(value, "Vobiz call UUID")
    if _UUID_PATTERN.fullmatch(call_uuid) is None:
        raise ValueError("Vobiz call UUID must be a canonical UUID")
    parsed = UUID(call_uuid)
    if parsed.int == 0:
        raise ValueError("Vobiz call UUID cannot be nil")
    return str(parsed)


def validate_vobiz_event_id(value: str) -> str:
    """Return a route- and SIP-safe durable event identifier."""

    event_id = _require_text(value, "event_id")
    if _EVENT_ID_PATTERN.fullmatch(event_id) is None:
        raise ValueError("event_id must be a safe evt_-prefixed identifier")
    return event_id


def build_answer_callback_url(
    *,
    public_base_url: str,
    event_id: str,
    correlation_secret: SecretInput,
) -> str:
    """Build the event-bound URL that returns XML after answer."""

    return _build_event_callback_url(
        public_base_url=public_base_url,
        path=VOBIZ_OUTBOUND_VOICE_PATH,
        event_id=event_id,
        correlation_secret=correlation_secret,
        purpose="vobiz-answer",
    )


def build_ring_callback_url(
    *,
    public_base_url: str,
    event_id: str,
    correlation_secret: SecretInput,
) -> str:
    """Build an event-bound ringing notification URL."""

    return _build_event_callback_url(
        public_base_url=public_base_url,
        path=VOBIZ_RING_CALLBACK_PATH,
        event_id=event_id,
        correlation_secret=correlation_secret,
        purpose="vobiz-ring",
    )


def build_hangup_callback_url(
    *,
    public_base_url: str,
    event_id: str,
    correlation_secret: SecretInput,
) -> str:
    """Build an event-bound terminal notification URL."""

    return _build_event_callback_url(
        public_base_url=public_base_url,
        path=VOBIZ_HANGUP_CALLBACK_PATH,
        event_id=event_id,
        correlation_secret=correlation_secret,
        purpose="vobiz-hangup",
    )


def build_answer_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
) -> str:
    return _build_event_route_signature(
        correlation_secret,
        event_id=event_id,
        purpose="vobiz-answer",
    )


def verify_answer_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    signature: str | None,
) -> bool:
    return _verify_event_route_signature(
        correlation_secret,
        event_id=event_id,
        signature=signature,
        purpose="vobiz-answer",
    )


def build_ring_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
) -> str:
    return _build_event_route_signature(
        correlation_secret,
        event_id=event_id,
        purpose="vobiz-ring",
    )


def verify_ring_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    signature: str | None,
) -> bool:
    return _verify_event_route_signature(
        correlation_secret,
        event_id=event_id,
        signature=signature,
        purpose="vobiz-ring",
    )


def build_hangup_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
) -> str:
    return _build_event_route_signature(
        correlation_secret,
        event_id=event_id,
        purpose="vobiz-hangup",
    )


def verify_hangup_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    signature: str | None,
) -> bool:
    return _verify_event_route_signature(
        correlation_secret,
        event_id=event_id,
        signature=signature,
        purpose="vobiz-hangup",
    )


def compute_vobiz_webhook_signature(
    *,
    callback_url: str,
    nonce: str,
    auth_token: SecretInput,
    version: Literal[2, 3] = 3,
) -> str:
    """Compute Vobiz's documented V2 or V3 callback signature.

    Vobiz signs the callback URL without its query string. V2 appends the
    random nonce directly; V3 inserts a period before the nonce. The account
    Auth Token is the key. Because the nonce is random rather than a timestamp,
    the HTTP boundary must also consume each accepted nonce only once.
    """

    base_url = _validate_vobiz_webhook_url(callback_url)
    normalized_nonce = _validate_vobiz_nonce(nonce)
    if version not in {2, 3}:
        raise ValueError("Vobiz signature version must be 2 or 3")
    token = _require_secret(auth_token, "VOBIZ_AUTH_TOKEN", min_length=16)
    separator = "." if version == 3 else ""
    digest = hmac.new(
        token.encode("utf-8"),
        f"{base_url}{separator}{normalized_nonce}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_vobiz_webhook_signature(
    *,
    callback_url: str,
    nonce: str | None,
    signature: str | None,
    auth_token: SecretInput,
    version: Literal[2, 3] = 3,
) -> bool:
    """Verify one Vobiz V2/V3 signature with a constant-time comparison."""

    if nonce is None or not isinstance(signature, str) or not signature:
        return False
    try:
        expected = compute_vobiz_webhook_signature(
            callback_url=callback_url,
            nonce=nonce,
            auth_token=auth_token,
            version=version,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def verify_vobiz_webhook_headers(
    *,
    callback_url: str,
    auth_token: SecretInput,
    signature_v3: str | None,
    nonce_v3: str | None,
    signature_v2: str | None,
    nonce_v2: str | None,
) -> VobizWebhookVerification | None:
    """Prefer V3; use V2 only when both V3 headers are wholly absent."""

    if signature_v3 is not None or nonce_v3 is not None:
        if not verify_vobiz_webhook_signature(
            callback_url=callback_url,
            nonce=nonce_v3,
            signature=signature_v3,
            auth_token=auth_token,
            version=3,
        ):
            return None
        assert nonce_v3 is not None
        return VobizWebhookVerification(version=3, nonce=nonce_v3)
    if not verify_vobiz_webhook_signature(
        callback_url=callback_url,
        nonce=nonce_v2,
        signature=signature_v2,
        auth_token=auth_token,
        version=2,
    ):
        return None
    assert nonce_v2 is not None
    return VobizWebhookVerification(version=2, nonce=nonce_v2)


def encode_vobiz_sip_header_value(value: str) -> str:
    """Encode arbitrary UTF-8 as canonical padding-free Base32."""

    text = _require_text(value, "Vobiz SIP header value")
    encoded = base64.b32encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    if len(encoded) > _MAX_ENCODED_HEADER_VALUE_BYTES:
        raise ValueError("Vobiz SIP header value exceeds the safe size limit")
    return encoded


def decode_vobiz_sip_header_value(value: str) -> str:
    """Decode a canonical value produced by :func:`encode_vobiz_sip_header_value`."""

    encoded = _require_text(value, "Vobiz SIP header value")
    if (
        len(encoded) > _MAX_ENCODED_HEADER_VALUE_BYTES
        or _VOBIZ_ENCODED_VALUE_PATTERN.fullmatch(encoded) is None
    ):
        raise ValueError("Vobiz SIP header value is not canonical Base32")
    padding = "=" * ((8 - len(encoded) % 8) % 8)
    try:
        decoded_bytes = base64.b32decode(encoded + padding, casefold=False)
        decoded = decoded_bytes.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise ValueError("Vobiz SIP header value is not canonical Base32") from None
    if not decoded or encode_vobiz_sip_header_value(decoded) != encoded:
        raise ValueError("Vobiz SIP header value is not canonical Base32")
    return decoded


def decode_vobiz_correlation_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Decode Vobiz ``X-VH-*`` metadata into canonical Hotline headers."""

    if not isinstance(headers, Mapping):
        raise TypeError("Vobiz SIP headers must be a mapping")
    decoded: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise TypeError("Vobiz SIP header names and values must be strings")
        canonical_name = _VOBIZ_TO_CANONICAL_CORRELATION_HEADERS.get(raw_name.strip().lower())
        if canonical_name is None:
            continue
        canonical_value = decode_vobiz_sip_header_value(raw_value.strip())
        existing = decoded.get(canonical_name)
        if existing is not None and not hmac.compare_digest(existing, canonical_value):
            raise ValueError("conflicting Vobiz correlation headers")
        decoded[canonical_name] = canonical_value
    return decoded


def build_outbound_bridge_xml(
    *,
    openai_project_id: str,
    event_id: str,
    correlation_secret: SecretInput,
    correlation_call_uuid: str,
    max_call_duration_seconds: int = 1800,
    dial_timeout_seconds: int = 30,
) -> str:
    """Build Vobiz XML binding an outbound event to its OpenAI SIP leg."""

    project_id = _validate_project_id(openai_project_id)
    normalized_event_id = validate_vobiz_event_id(event_id)
    call_uuid = validate_vobiz_call_uuid(correlation_call_uuid)
    secret = _require_secret(
        correlation_secret,
        "HOTLINE_SIP_CORRELATION_SECRET",
        min_length=16,
    )
    time_limit = _validate_seconds(
        max_call_duration_seconds,
        "max_call_duration_seconds",
        minimum=60,
        maximum=7200,
    )
    dial_timeout = _validate_seconds(
        dial_timeout_seconds,
        "dial_timeout_seconds",
        minimum=5,
        maximum=600,
    )
    signature = build_correlation_signature(
        secret,
        direction="outbound",
        call_sid=call_uuid,
        event_id=normalized_event_id,
    )
    return _build_bridge_xml(
        project_id,
        {
            "X-Hotline-Direction": "outbound",
            "X-Hotline-Call-Sid": call_uuid,
            "X-Hotline-Event-Id": normalized_event_id,
            "X-Hotline-Signature": signature,
        },
        max_call_duration_seconds=time_limit,
        dial_timeout_seconds=dial_timeout,
    )


def build_inbound_bridge_xml(
    *,
    openai_project_id: str,
    call_uuid: str,
    caller_phone: str,
    admission_nonce: str,
    expires_at_epoch: int,
    correlation_secret: SecretInput,
    max_call_duration_seconds: int = 1800,
    dial_timeout_seconds: int = 30,
) -> str:
    """Build Vobiz XML binding an admitted inbound caller to OpenAI SIP."""

    project_id = _validate_project_id(openai_project_id)
    normalized_call_uuid = validate_vobiz_call_uuid(call_uuid)
    normalized_caller = normalize_e164(caller_phone)
    secret = _require_secret(
        correlation_secret,
        "HOTLINE_SIP_CORRELATION_SECRET",
        min_length=16,
    )
    time_limit = _validate_seconds(
        max_call_duration_seconds,
        "max_call_duration_seconds",
        minimum=60,
        maximum=7200,
    )
    dial_timeout = _validate_seconds(
        dial_timeout_seconds,
        "dial_timeout_seconds",
        minimum=5,
        maximum=600,
    )
    signature = build_correlation_signature(
        secret,
        direction="inbound",
        call_sid=normalized_call_uuid,
        caller_phone=normalized_caller,
        admission_nonce=admission_nonce,
        expires_at_epoch=expires_at_epoch,
    )
    return _build_bridge_xml(
        project_id,
        {
            "X-Hotline-Direction": "inbound",
            "X-Hotline-Call-Sid": normalized_call_uuid,
            "X-Hotline-Caller": normalized_caller,
            "X-Hotline-Admission": admission_nonce,
            "X-Hotline-Expires": str(expires_at_epoch),
            "X-Hotline-Signature": signature,
        },
        max_call_duration_seconds=time_limit,
        dial_timeout_seconds=dial_timeout,
    )


class VobizClient:
    """Minimal async client for Vobiz's Calls REST resource."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self._auth_id = validate_vobiz_auth_id(settings.vobiz_auth_id)
        self._auth_token = _require_secret(
            settings.vobiz_auth_token,
            "VOBIZ_AUTH_TOKEN",
            min_length=16,
        )
        if not self._auth_token.isascii():
            raise ValueError("VOBIZ_AUTH_TOKEN must contain only ASCII characters")
        self._from_number = normalize_e164(
            _require_text(settings.vobiz_phone_number, "VOBIZ_PHONE_NUMBER")
        )
        self._owner_number = normalize_e164(
            _require_secret(settings.owner_phone_number, "OWNER_PHONE_NUMBER")
        )
        self._public_base_url = _validate_public_base_url(
            _require_text(settings.public_base_url, "PUBLIC_BASE_URL")
        )
        self._correlation_secret = _require_secret(
            settings.hotline_sip_correlation_secret,
            "HOTLINE_SIP_CORRELATION_SECRET",
            min_length=16,
        )
        self._max_call_duration_seconds = _validate_seconds(
            settings.hotline_max_call_duration_seconds,
            "HOTLINE_MAX_CALL_DURATION_SECONDS",
            minimum=60,
            maximum=7200,
        )
        self._outbound_ring_timeout_seconds = _validate_seconds(
            settings.hotline_outbound_ring_timeout_seconds,
            "HOTLINE_OUTBOUND_RING_TIMEOUT_SECONDS",
            minimum=5,
            maximum=600,
        )
        self._retry_attempts = _validate_retry_attempts(settings.hotline_retry_attempts)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
        )

    async def __aenter__(self) -> VobizClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def probe(self) -> None:
        """Perform a read-only credential check using the live-call list."""

        try:
            response = await self._client.get(
                self._calls_url,
                params={"status": "live"},
                headers=self._headers,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise VobizAPIError(
                f"Vobiz account probe failed: {type(exc).__name__}",
                retriable=True,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise VobizAPIError(
                f"Vobiz returned HTTP {response.status_code}",
                status_code=response.status_code,
            )

    async def place_call(
        self,
        event_id: str,
        request: object | None = None,
    ) -> VobizCallResult:
        """Call the configured owner and fetch Voice XML after answer."""

        del request
        normalized_event_id = validate_vobiz_event_id(event_id)
        payload: dict[str, str | int] = {
            "from": self._from_number,
            "to": self._owner_number,
            "answer_url": build_answer_callback_url(
                public_base_url=self._public_base_url,
                event_id=normalized_event_id,
                correlation_secret=self._correlation_secret,
            ),
            "answer_method": "POST",
            "ring_url": build_ring_callback_url(
                public_base_url=self._public_base_url,
                event_id=normalized_event_id,
                correlation_secret=self._correlation_secret,
            ),
            "ring_method": "POST",
            "hangup_url": build_hangup_callback_url(
                public_base_url=self._public_base_url,
                event_id=normalized_event_id,
                correlation_secret=self._correlation_secret,
            ),
            "hangup_method": "POST",
            "time_limit": self._max_call_duration_seconds,
            "hangup_on_ring": self._outbound_ring_timeout_seconds,
        }
        response = await self._post_call(payload)
        return _parse_call_result(response)

    async def end_call(self, call_uuid: str) -> None:
        """Idempotently terminate a Vobiz call using its request UUID."""

        normalized_uuid = validate_vobiz_call_uuid(call_uuid)
        url = f"{self._calls_url}{normalized_uuid}/"
        for attempt in range(self._retry_attempts + 1):
            try:
                response = await self._client.delete(
                    url,
                    headers=self._headers,
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                if attempt >= self._retry_attempts:
                    raise VobizAPIError(
                        "Vobiz call termination could not be confirmed",
                        outcome_unknown=True,
                    ) from exc
                await self._sleep(min(0.25 * (2**attempt), 2.0))
                continue
            if 200 <= response.status_code < 300 or response.status_code == 404:
                return
            transient = response.status_code in {408, 429} or response.status_code >= 500
            if transient and attempt < self._retry_attempts:
                await self._sleep(min(0.25 * (2**attempt), 2.0))
                continue
            raise VobizAPIError(
                f"Vobiz call termination returned HTTP {response.status_code}",
                status_code=response.status_code,
                outcome_unknown=transient,
            )

    async def _post_call(self, payload: Mapping[str, str | int]) -> httpx.Response:
        for attempt in range(self._retry_attempts + 1):
            try:
                response = await self._client.post(
                    self._calls_url,
                    json=payload,
                    headers=self._headers,
                    follow_redirects=False,
                )
            except _SAFE_PRE_SEND_ERRORS:
                if attempt >= self._retry_attempts:
                    raise VobizAPIError(
                        "Vobiz call request could not connect",
                        retriable=True,
                    ) from None
                delay = min(0.25 * (2**attempt), 2.0)
                delay += self._rng.uniform(0.0, min(delay * 0.2, 0.25))
                logger.warning(
                    "Retrying Vobiz call creation after a pre-send connection failure",
                    extra={
                        "attempt": attempt + 1,
                        "delay_seconds": round(delay, 3),
                    },
                )
                await self._sleep(delay)
                continue
            except httpx.RequestError:
                # Delivery may have happened; retrying could ring the owner twice.
                raise VobizAPIError(
                    "Vobiz call request failed with an unknown delivery outcome",
                    outcome_unknown=True,
                ) from None

            if not 200 <= response.status_code < 300:
                raise VobizAPIError(
                    f"Vobiz returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    outcome_unknown=response.status_code >= 500,
                )
            return response
        raise VobizAPIError("Vobiz call request failed")

    @property
    def _calls_url(self) -> str:
        return f"{VOBIZ_API_BASE_URL}/Account/{self._auth_id}/Call/"

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "X-Auth-ID": self._auth_id,
            "X-Auth-Token": self._auth_token,
            "Accept": "application/json",
        }


def _build_event_callback_url(
    *,
    public_base_url: str,
    path: str,
    event_id: str,
    correlation_secret: SecretInput,
    purpose: Literal["vobiz-answer", "vobiz-ring", "vobiz-hangup"],
) -> str:
    base_url = _validate_public_base_url(public_base_url)
    normalized_event_id = validate_vobiz_event_id(event_id)
    signature = _build_event_route_signature(
        correlation_secret,
        event_id=normalized_event_id,
        purpose=purpose,
    )
    query = urlencode({"event_id": normalized_event_id, "event_sig": signature})
    callback_url = f"{base_url}{path}?{query}"
    if len(callback_url.encode("ascii")) > _MAX_CALLBACK_URL_BYTES:
        raise ValueError("Vobiz callback URL exceeds the safe size limit")
    return callback_url


def _build_event_route_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    purpose: Literal["vobiz-answer", "vobiz-ring", "vobiz-hangup"],
) -> str:
    secret = _require_secret(
        correlation_secret,
        "HOTLINE_SIP_CORRELATION_SECRET",
        min_length=16,
    )
    normalized_event_id = validate_vobiz_event_id(event_id)
    payload = f"agent-hotline:{purpose}:v1\n{normalized_event_id}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _verify_event_route_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    signature: str | None,
    purpose: Literal["vobiz-answer", "vobiz-ring", "vobiz-hangup"],
) -> bool:
    if not isinstance(signature, str) or not signature:
        return False
    try:
        expected = _build_event_route_signature(
            correlation_secret,
            event_id=event_id,
            purpose=purpose,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def _build_bridge_xml(
    project_id: str,
    headers: Mapping[str, str],
    *,
    max_call_duration_seconds: int,
    dial_timeout_seconds: int,
) -> str:
    sip_uri = f"sip:{project_id}@{OPENAI_SIP_HOST}:{OPENAI_SIP_PORT};transport=tls"
    if len(sip_uri.encode("ascii")) >= 255:
        raise ValueError("OpenAI SIP URI exceeds the safe size limit")

    encoded_headers: list[str] = []
    for raw_name, value in headers.items():
        vobiz_name = _CANONICAL_TO_VOBIZ_CORRELATION_HEADERS.get(raw_name.lower())
        if vobiz_name is None:
            raise ValueError("unsupported Vobiz correlation header")
        encoded_headers.append(f"{vobiz_name}={encode_vobiz_sip_header_value(value)}")
    sip_headers = ",".join(encoded_headers)
    if len(sip_headers.encode("ascii")) >= _MAX_SIP_HEADERS_BYTES:
        raise ValueError("Vobiz custom SIP headers exceed the safe size limit")

    response = ElementTree.Element("Response")
    dial = ElementTree.SubElement(
        response,
        "Dial",
        {
            "timeLimit": str(max_call_duration_seconds),
            "timeout": str(dial_timeout_seconds),
        },
    )
    user = ElementTree.SubElement(dial, "User", {"sipHeaders": sip_headers})
    user.text = sip_uri
    ElementTree.SubElement(response, "Hangup")
    body = ElementTree.tostring(response, encoding="unicode", short_empty_elements=True)
    xml = f'<?xml version="1.0" encoding="UTF-8"?>{body}'
    if len(xml.encode("utf-8")) > 4000:
        raise ValueError("inline Vobiz XML exceeds the safe size limit")
    return xml


def _parse_call_result(response: httpx.Response) -> VobizCallResult:
    try:
        body: Any = response.json()
    except ValueError:
        raise VobizAPIError(
            "Vobiz returned an invalid JSON response",
            status_code=response.status_code,
            outcome_unknown=200 <= response.status_code < 300,
        ) from None
    if not isinstance(body, dict):
        raise VobizAPIError(
            "Vobiz returned a non-object JSON response",
            status_code=response.status_code,
            outcome_unknown=200 <= response.status_code < 300,
        )
    request_uuid = body.get("request_uuid")
    try:
        normalized_uuid = validate_vobiz_call_uuid(request_uuid)
    except (TypeError, ValueError):
        raise VobizAPIError(
            "Vobiz response did not contain a valid request_uuid",
            status_code=response.status_code,
            outcome_unknown=200 <= response.status_code < 300,
        ) from None
    return VobizCallResult(attempt_id=normalized_uuid)


def _validate_vobiz_nonce(value: str) -> str:
    nonce = _require_text(value, "Vobiz signature nonce")
    if _VOBIZ_NONCE_PATTERN.fullmatch(nonce) is None:
        raise ValueError("Vobiz signature nonce must contain exactly 20 ASCII digits")
    return nonce


def _validate_vobiz_webhook_url(value: str) -> str:
    exact_url = _require_text(value, "Vobiz webhook URL")
    _reject_unsafe_url_characters(exact_url, "Vobiz webhook URL")
    try:
        parsed = urlsplit(exact_url)
        if parsed.port == 0 or parsed.netloc.endswith(":"):
            raise ValueError
    except ValueError:
        raise ValueError("Vobiz webhook URL is not valid") from None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("Vobiz webhook URL must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Vobiz webhook URL cannot contain user information")
    if parsed.fragment:
        raise ValueError("Vobiz webhook URL cannot contain a fragment")
    _validate_hostname(parsed.hostname, "Vobiz webhook URL")
    # Vobiz V2/V3 deliberately excludes the query string from its HMAC input.
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _validate_project_id(value: str | None) -> str:
    project_id = _require_text(value, "OPENAI_PROJECT_ID")
    if _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise ValueError("OPENAI_PROJECT_ID must be a safe proj_-prefixed identifier")
    return project_id


def _validate_retry_attempts(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 5:
        raise ValueError("HOTLINE_RETRY_ATTEMPTS must be between 0 and 5")
    return value


def _validate_seconds(
    value: int,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum} seconds")
    return value


def _validate_public_base_url(value: str) -> str:
    base_url = _require_text(value, "PUBLIC_BASE_URL")
    _reject_unsafe_url_characters(base_url, "PUBLIC_BASE_URL")
    try:
        parsed = urlsplit(base_url)
        if parsed.port == 0 or parsed.netloc.endswith(":"):
            raise ValueError
    except ValueError:
        raise ValueError("PUBLIC_BASE_URL is not a valid URL") from None
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("PUBLIC_BASE_URL must be an absolute HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("PUBLIC_BASE_URL cannot contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("PUBLIC_BASE_URL cannot contain a query string or fragment")
    _validate_hostname(parsed.hostname, "PUBLIC_BASE_URL")
    return base_url.rstrip("/")


def _validate_hostname(hostname: str, label: str) -> None:
    if not hostname.isascii():
        raise ValueError(f"{label} must contain a valid hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        dns_name = hostname[:-1] if hostname.endswith(".") else hostname
        labels = dns_name.split(".")
        if (
            not dns_name
            or len(dns_name) > 253
            or any(_HOST_LABEL_PATTERN.fullmatch(item) is None for item in labels)
        ):
            raise ValueError(f"{label} must contain a valid hostname") from None


def _require_text(value: str | None, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is not configured")
    result = value.strip()
    if not result:
        raise ValueError(f"{label} is not configured")
    if result != value:
        raise ValueError(f"{label} cannot contain surrounding whitespace")
    _reject_control_characters(result, label)
    return result


def _require_secret(
    value: SecretInput,
    label: str,
    *,
    min_length: int = 1,
) -> str:
    if isinstance(value, str):
        secret = value
    else:
        getter = getattr(value, "get_secret_value", None)
        if not callable(getter):
            raise TypeError(f"{label} must be a string or secret value")
        secret = getter()
    if not isinstance(secret, str) or len(secret) < min_length:
        raise ValueError(f"{label} is not configured or is too short")
    if secret != secret.strip():
        raise ValueError(f"{label} cannot contain surrounding whitespace")
    _reject_control_characters(secret, label)
    return secret


def _reject_control_characters(value: str, label: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} cannot contain control characters")


def _reject_unsafe_url_characters(value: str, label: str) -> None:
    _reject_control_characters(value, label)
    if not value.isascii() or "\\" in value or any(character.isspace() for character in value):
        raise ValueError(f"{label} must use an ASCII, percent-encoded URL")


# Descriptive aliases for integrations that prefer carrier-qualified helper names.
build_outbound_answer_url = build_answer_callback_url
build_outbound_ring_url = build_ring_callback_url
build_outbound_hangup_url = build_hangup_callback_url
build_outbound_bridge_vobiz_xml = build_outbound_bridge_xml
build_inbound_bridge_vobiz_xml = build_inbound_bridge_xml
validate_vobiz_call_id = validate_vobiz_call_uuid


__all__ = [
    "VOBIZ_API_BASE_URL",
    "VOBIZ_HANGUP_CALLBACK_PATH",
    "VOBIZ_INBOUND_VOICE_PATH",
    "VOBIZ_OUTBOUND_VOICE_PATH",
    "VOBIZ_RING_CALLBACK_PATH",
    "VOBIZ_STATUS_CALLBACK_PATH",
    "VobizAPIError",
    "VobizCallResult",
    "VobizClient",
    "VobizWebhookVerification",
    "build_answer_callback_url",
    "build_answer_event_signature",
    "build_hangup_callback_url",
    "build_hangup_event_signature",
    "build_inbound_bridge_vobiz_xml",
    "build_inbound_bridge_xml",
    "build_outbound_answer_url",
    "build_outbound_bridge_vobiz_xml",
    "build_outbound_bridge_xml",
    "build_outbound_hangup_url",
    "build_outbound_ring_url",
    "build_ring_callback_url",
    "build_ring_event_signature",
    "compute_vobiz_webhook_signature",
    "normalize_e164",
    "validate_vobiz_auth_id",
    "validate_vobiz_call_id",
    "validate_vobiz_call_uuid",
    "validate_vobiz_event_id",
    "verify_answer_event_signature",
    "verify_hangup_event_signature",
    "verify_ring_event_signature",
    "verify_vobiz_webhook_headers",
    "verify_vobiz_webhook_signature",
]
