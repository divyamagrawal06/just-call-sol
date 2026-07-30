"""Twilio carrier transport for bridging PSTN calls to OpenAI Realtime SIP."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

import httpx

from .telephony_security import (
    build_correlation_signature,
    verify_correlation_signature,
)

if TYPE_CHECKING:
    from .settings import Settings

logger = logging.getLogger(__name__)

TWILIO_CALLS_BASE_URL = "https://api.twilio.com/2010-04-01/Accounts"
TWILIO_OUTBOUND_VOICE_PATH = "/v1/twilio/voice/outbound"
TWILIO_STATUS_CALLBACK_PATH = "/v1/twilio/status"
OPENAI_SIP_HOST = "sip.api.openai.com"

_ACCOUNT_SID_PATTERN = re.compile(r"^AC[0-9a-fA-F]{32}$")
_CALL_SID_PATTERN = re.compile(r"^CA[0-9a-fA-F]{32}$")
_EVENT_ID_PATTERN = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$")
_PROJECT_ID_PATTERN = re.compile(r"^proj_[A-Za-z0-9_-]{1,128}$")
_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")
_HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PHONE_SEPARATORS = frozenset(" .()-")
_SAFE_PRE_SEND_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_STATUS_CALLBACK_EVENTS = ("initiated", "ringing", "answered", "completed")


class _SecretValue(Protocol):
    def get_secret_value(self) -> str: ...


SecretInput = str | _SecretValue
WebhookParameterValue = str | Sequence[str]
WebhookParameters = Mapping[str, WebhookParameterValue] | Iterable[tuple[str, str]] | None


class TwilioAPIError(RuntimeError):
    """A Twilio failure whose message is safe to expose in logs."""

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
class TwilioCallResult:
    """The carrier result, structurally compatible with ``CallAttempt``."""

    attempt_id: str
    provider: Literal["twilio"] = "twilio"


def normalize_e164(value: str) -> str:
    """Normalize common visual separators and return a strict E.164 number."""

    if not isinstance(value, str):
        raise TypeError("phone number must be a string")
    _reject_control_characters(value, "phone number")
    stripped = value.strip()
    normalized = "".join(character for character in stripped if character not in _PHONE_SEPARATORS)
    if _E164_PATTERN.fullmatch(normalized) is None:
        raise ValueError("phone number must be E.164, for example +12025550123")
    return normalized


def validate_twilio_account_sid(value: str) -> str:
    """Return one strict Twilio AccountSid or raise before durable correlation."""

    return _validate_account_sid(value)


def validate_twilio_call_sid(value: str) -> str:
    """Return one strict Twilio CallSid or raise before durable correlation."""

    return _validate_call_sid(value)


def build_status_callback_url(
    *,
    public_base_url: str,
    event_id: str | None = None,
    correlation_secret: SecretInput | None = None,
) -> str:
    """Build the HTTPS endpoint authenticated by ``X-Twilio-Signature``."""

    base_url = _validate_public_base_url(public_base_url)
    callback_url = f"{base_url}{TWILIO_STATUS_CALLBACK_PATH}"
    if event_id is None and correlation_secret is None:
        return callback_url
    if event_id is None or correlation_secret is None:
        raise ValueError("event_id and correlation_secret must be supplied together")
    normalized_event_id = _validate_event_id(event_id)
    signature = build_status_event_signature(
        correlation_secret,
        event_id=normalized_event_id,
    )
    return f"{callback_url}?{urlencode({'event_id': normalized_event_id, 'event_sig': signature})}"


def build_outbound_voice_url(
    *,
    public_base_url: str,
    event_id: str,
    correlation_secret: SecretInput,
) -> str:
    """Build the signed TwiML URL Twilio fetches after allocating the parent CallSid."""

    base_url = _validate_public_base_url(public_base_url)
    normalized_event_id = _validate_event_id(event_id)
    signature = build_outbound_voice_event_signature(
        correlation_secret,
        event_id=normalized_event_id,
    )
    query = urlencode({"event_id": normalized_event_id, "event_sig": signature})
    return f"{base_url}{TWILIO_OUTBOUND_VOICE_PATH}?{query}"


def build_status_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
) -> str:
    """Bind an outbound status callback URL to one durable event."""

    return _build_event_route_signature(
        correlation_secret,
        event_id=event_id,
        purpose="twilio-status",
    )


def verify_status_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    signature: str | None,
) -> bool:
    """Verify an event-bound status callback correlation value."""

    if not isinstance(signature, str) or not signature:
        return False
    try:
        expected = build_status_event_signature(
            correlation_secret,
            event_id=event_id,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def build_outbound_voice_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
) -> str:
    """Bind an outbound TwiML request URL to one durable event."""

    return _build_event_route_signature(
        correlation_secret,
        event_id=event_id,
        purpose="twilio-outbound-voice",
    )


def verify_outbound_voice_event_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    signature: str | None,
) -> bool:
    if not isinstance(signature, str) or not signature:
        return False
    try:
        expected = build_outbound_voice_event_signature(
            correlation_secret,
            event_id=event_id,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


def _build_event_route_signature(
    correlation_secret: SecretInput,
    *,
    event_id: str,
    purpose: Literal["twilio-status", "twilio-outbound-voice"],
) -> str:
    secret = _require_secret(
        correlation_secret,
        "HOTLINE_SIP_CORRELATION_SECRET",
        min_length=16,
    )
    normalized_event_id = _validate_event_id(event_id)
    payload = f"agent-hotline:{purpose}:v1\n{normalized_event_id}".encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_outbound_bridge_twiml(
    *,
    openai_project_id: str,
    event_id: str,
    correlation_secret: SecretInput,
    correlation_call_sid: str,
    max_call_duration_seconds: int = 1800,
    dial_timeout_seconds: int = 30,
) -> str:
    """Build inline TwiML that binds an outbound event to the OpenAI SIP leg.

    Twilio fetches this document only after allocating the outbound parent
    ``CallSid``. The daemon signs that actual parent ID into the custom SIP
    headers; the SIP noun's standard Twilio CallSid is a different child leg.
    """

    project_id = _validate_project_id(openai_project_id)
    normalized_event_id = _validate_event_id(event_id)
    signing_secret = _require_secret(
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
    correlation_id = _validate_call_sid(correlation_call_sid)
    signature = build_correlation_signature(
        signing_secret,
        direction="outbound",
        call_sid=correlation_id,
        event_id=normalized_event_id,
    )
    return _build_bridge_twiml(
        project_id,
        {
            "X-Hotline-Direction": "outbound",
            "X-Hotline-Call-Sid": correlation_id,
            "X-Hotline-Event-Id": normalized_event_id,
            "X-Hotline-Signature": signature,
        },
        max_call_duration_seconds=time_limit,
        dial_timeout_seconds=dial_timeout,
    )


def build_inbound_bridge_twiml(
    *,
    openai_project_id: str,
    call_sid: str,
    caller_phone: str,
    admission_nonce: str,
    expires_at_epoch: int,
    public_base_url: str,
    correlation_secret: SecretInput,
    max_call_duration_seconds: int = 1800,
    dial_timeout_seconds: int = 30,
) -> str:
    """Build TwiML that binds a verified inbound Twilio call to its SIP leg."""

    project_id = _validate_project_id(openai_project_id)
    normalized_call_sid = _validate_call_sid(call_sid)
    normalized_caller = normalize_e164(caller_phone)
    signing_secret = _require_secret(
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
        signing_secret,
        direction="inbound",
        call_sid=normalized_call_sid,
        caller_phone=normalized_caller,
        admission_nonce=admission_nonce,
        expires_at_epoch=expires_at_epoch,
    )
    return _build_bridge_twiml(
        project_id,
        {
            "X-Hotline-Direction": "inbound",
            "X-Hotline-Call-Sid": normalized_call_sid,
            "X-Hotline-Caller": normalized_caller,
            "X-Hotline-Admission": admission_nonce,
            "X-Hotline-Expires": str(expires_at_epoch),
            "X-Hotline-Signature": signature,
        },
        dial_action_url=build_status_callback_url(public_base_url=public_base_url),
        max_call_duration_seconds=time_limit,
        dial_timeout_seconds=dial_timeout,
    )


def compute_twilio_webhook_signature(
    *,
    url: str,
    params: WebhookParameters,
    auth_token: SecretInput,
) -> str:
    """Compute Twilio's HMAC-SHA1 signature for form-encoded webhooks.

    ``url`` must be the exact externally visible URL Twilio requested, including
    its encoded query string. Query parameters must not also be passed in
    ``params``. Repeated form values follow Twilio's official helper behavior:
    unique names and values are sorted using case-sensitive ordering.
    """

    exact_url = _validate_webhook_url(url)
    token = _require_secret(auth_token, "TWILIO_AUTH_TOKEN")
    canonical = exact_url
    for name, values in _group_webhook_parameters(params):
        for value in values:
            canonical += name + value
    digest = hmac.new(
        token.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_twilio_webhook_signature(
    *,
    url: str,
    params: WebhookParameters,
    signature: str | None,
    auth_token: SecretInput,
) -> bool:
    """Verify ``X-Twilio-Signature`` using a constant-time comparison."""

    if not isinstance(signature, str) or not signature:
        return False
    try:
        expected = compute_twilio_webhook_signature(
            url=url,
            params=params,
            auth_token=auth_token,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, signature)


class TwilioClient:
    """Minimal async client for Twilio's outbound Calls REST resource."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings
        self._account_sid = _validate_account_sid(settings.twilio_account_sid)
        self._auth_token = _require_secret(
            settings.twilio_auth_token,
            "TWILIO_AUTH_TOKEN",
            min_length=16,
        )
        self._from_number = normalize_e164(
            _require_text(settings.twilio_phone_number, "TWILIO_PHONE_NUMBER")
        )
        self._owner_number = normalize_e164(
            _require_secret(settings.owner_phone_number, "OWNER_PHONE_NUMBER")
        )
        self._project_id = _validate_project_id(settings.openai_project_id)
        self._correlation_secret = _require_secret(
            settings.hotline_sip_correlation_secret,
            "HOTLINE_SIP_CORRELATION_SECRET",
            min_length=16,
        )
        self._public_base_url = _validate_public_base_url(
            _require_text(settings.public_base_url, "PUBLIC_BASE_URL")
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

    async def __aenter__(self) -> TwilioClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def probe(self) -> None:
        """Perform a read-only credential/account check for ``doctor --live``."""

        url = f"{TWILIO_CALLS_BASE_URL}/{self._account_sid}.json"
        try:
            response = await self._client.get(
                url,
                auth=httpx.BasicAuth(self._account_sid, self._auth_token),
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise TwilioAPIError(
                f"Twilio account probe failed: {type(exc).__name__}",
                retriable=True,
            ) from exc
        if not 200 <= response.status_code < 300:
            raise TwilioAPIError(
                f"Twilio returned HTTP {response.status_code}",
                status_code=response.status_code,
                retriable=False,
            )

    async def place_call(
        self,
        event_id: str,
        request: object | None = None,
    ) -> TwilioCallResult:
        """Call the configured owner and bridge the answered leg to OpenAI SIP."""

        del request
        form: dict[str, str | tuple[str, ...]] = {
            "To": self._owner_number,
            "From": self._from_number,
            "Url": build_outbound_voice_url(
                public_base_url=self._public_base_url,
                event_id=event_id,
                correlation_secret=self._correlation_secret,
            ),
            "Method": "POST",
            "StatusCallback": build_status_callback_url(
                public_base_url=self._public_base_url,
                event_id=event_id,
                correlation_secret=self._correlation_secret,
            ),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": _STATUS_CALLBACK_EVENTS,
            "TimeLimit": str(self._max_call_duration_seconds),
            "Timeout": str(self._outbound_ring_timeout_seconds),
        }
        response = await self._post_call(form)
        return _parse_call_result(response)

    async def end_call(self, call_sid: str) -> None:
        """Best-effort idempotent termination fallback for a Twilio parent call."""

        normalized_call_sid = _validate_call_sid(call_sid)
        url = f"{TWILIO_CALLS_BASE_URL}/{self._account_sid}/Calls/{normalized_call_sid}.json"
        for attempt in range(self._retry_attempts + 1):
            try:
                response = await self._client.post(
                    url,
                    data={"Status": "completed"},
                    auth=httpx.BasicAuth(self._account_sid, self._auth_token),
                    headers={"Accept": "application/json"},
                    follow_redirects=False,
                )
            except httpx.HTTPError as exc:
                if attempt >= self._retry_attempts:
                    raise TwilioAPIError(
                        "Twilio call termination could not be confirmed",
                        retriable=False,
                    ) from exc
                await self._sleep(min(0.25 * (2**attempt), 2.0))
                continue
            if 200 <= response.status_code < 300:
                return
            if (
                response.status_code in {408, 429} or response.status_code >= 500
            ) and attempt < self._retry_attempts:
                await self._sleep(min(0.25 * (2**attempt), 2.0))
                continue
            if response.status_code in {400, 404, 409} and await self._call_is_terminal(
                url,
            ):
                return
            raise TwilioAPIError(
                f"Twilio call termination returned HTTP {response.status_code}",
                status_code=response.status_code,
                retriable=False,
            )

    async def _call_is_terminal(self, url: str) -> bool:
        """Confirm an idempotent replay without retaining Twilio response data."""

        try:
            response = await self._client.get(
                url,
                auth=httpx.BasicAuth(self._account_sid, self._auth_token),
                headers={"Accept": "application/json"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise TwilioAPIError(
                "Twilio call state could not be confirmed",
                retriable=False,
            ) from exc
        if response.status_code == 404:
            return True
        if not 200 <= response.status_code < 300:
            return False
        try:
            payload = response.json()
        except ValueError:
            return False
        return isinstance(payload, Mapping) and payload.get("status") in {
            "busy",
            "canceled",
            "completed",
            "failed",
            "no-answer",
        }

    async def _post_call(
        self,
        form: Mapping[str, str | tuple[str, ...]],
    ) -> httpx.Response:
        url = f"{TWILIO_CALLS_BASE_URL}/{self._account_sid}/Calls.json"
        for attempt in range(self._retry_attempts + 1):
            try:
                response = await self._client.post(
                    url,
                    data=form,
                    auth=httpx.BasicAuth(self._account_sid, self._auth_token),
                    headers={"Accept": "application/json"},
                    follow_redirects=False,
                )
            except _SAFE_PRE_SEND_ERRORS:
                if attempt >= self._retry_attempts:
                    raise TwilioAPIError(
                        "Twilio call request could not connect",
                        retriable=True,
                    ) from None
                delay = min(0.25 * (2**attempt), 2.0)
                delay += self._rng.uniform(0.0, min(delay * 0.2, 0.25))
                logger.warning(
                    "Retrying Twilio call creation after a pre-send connection failure",
                    extra={
                        "attempt": attempt + 1,
                        "delay_seconds": round(delay, 3),
                    },
                )
                await self._sleep(delay)
                continue
            except httpx.RequestError:
                # Read/write/protocol failures have an ambiguous outcome. Retrying
                # the non-idempotent Calls POST could ring the owner twice.
                raise TwilioAPIError(
                    "Twilio call request failed with an unknown delivery outcome",
                    retriable=False,
                    outcome_unknown=True,
                ) from None

            if not 200 <= response.status_code < 300:
                # Even a transient response is not automatically safe to retry:
                # Twilio has received this non-idempotent call-creation request.
                raise TwilioAPIError(
                    f"Twilio returned HTTP {response.status_code}",
                    status_code=response.status_code,
                    retriable=False,
                    outcome_unknown=response.status_code >= 500,
                )
            return response

        raise TwilioAPIError("Twilio call request failed", retriable=False)


def _build_bridge_twiml(
    project_id: str,
    headers: Mapping[str, str],
    *,
    dial_action_url: str | None = None,
    max_call_duration_seconds: int,
    dial_timeout_seconds: int,
) -> str:
    base_uri = f"sip:{project_id}@{OPENAI_SIP_HOST};transport=tls"
    if len(base_uri.encode("utf-8")) >= 255:
        raise ValueError("OpenAI SIP URI exceeds Twilio's 255-byte limit")
    query = urlencode(list(headers.items()), quote_via=quote, safe="")
    if len(query.encode("utf-8")) >= 1024:
        raise ValueError("custom SIP headers exceed Twilio's 1024-byte limit")
    sip_uri = f"{base_uri}?{query}"

    response = ElementTree.Element("Response")
    dial_attributes = {
        "timeLimit": str(max_call_duration_seconds),
        "timeout": str(dial_timeout_seconds),
    }
    if dial_action_url is not None:
        dial_attributes.update(
            {
                "action": _validate_webhook_url(dial_action_url),
                "method": "POST",
            }
        )
    dial = ElementTree.SubElement(response, "Dial", dial_attributes)
    sip = ElementTree.SubElement(dial, "Sip")
    sip.text = sip_uri
    body = ElementTree.tostring(
        response,
        encoding="unicode",
        short_empty_elements=True,
    )
    twiml = f'<?xml version="1.0" encoding="UTF-8"?>{body}'
    if len(twiml.encode("utf-8")) > 4000:
        raise ValueError("inline TwiML exceeds Twilio's 4000-byte limit")
    return twiml


def _parse_call_result(response: httpx.Response) -> TwilioCallResult:
    try:
        body: Any = response.json()
    except ValueError:
        raise TwilioAPIError(
            "Twilio returned an invalid JSON response",
            status_code=response.status_code,
            outcome_unknown=200 <= response.status_code < 300,
        ) from None
    if not isinstance(body, dict):
        raise TwilioAPIError(
            "Twilio returned a non-object JSON response",
            status_code=response.status_code,
            outcome_unknown=200 <= response.status_code < 300,
        )
    call_sid = body.get("sid")
    if not isinstance(call_sid, str) or _CALL_SID_PATTERN.fullmatch(call_sid) is None:
        raise TwilioAPIError(
            "Twilio response did not contain a valid CallSid",
            status_code=response.status_code,
            outcome_unknown=200 <= response.status_code < 300,
        )
    return TwilioCallResult(attempt_id=call_sid)


def _validate_account_sid(value: str | None) -> str:
    account_sid = _require_text(value, "TWILIO_ACCOUNT_SID")
    if _ACCOUNT_SID_PATTERN.fullmatch(account_sid) is None:
        raise ValueError("TWILIO_ACCOUNT_SID must be an AC-prefixed Twilio SID")
    return account_sid


def _validate_call_sid(value: str) -> str:
    call_sid = _require_text(value, "CallSid")
    if _CALL_SID_PATTERN.fullmatch(call_sid) is None:
        raise ValueError("CallSid must be a CA-prefixed Twilio SID")
    return call_sid


def _validate_event_id(value: str) -> str:
    event_id = _require_text(value, "event_id")
    if _EVENT_ID_PATTERN.fullmatch(event_id) is None:
        raise ValueError("event_id must be a safe evt_-prefixed identifier")
    return event_id


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


def _validate_webhook_url(value: str) -> str:
    exact_url = _require_text(value, "Twilio webhook URL", preserve_whitespace=True)
    _reject_unsafe_url_characters(exact_url, "Twilio webhook URL")
    try:
        parsed = urlsplit(exact_url)
        if parsed.port == 0 or parsed.netloc.endswith(":"):
            raise ValueError
    except ValueError:
        raise ValueError("Twilio webhook URL is not valid") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Twilio webhook URL must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Twilio webhook URL cannot contain user information")
    if parsed.fragment:
        raise ValueError("Twilio webhook URL cannot contain a fragment")
    _validate_hostname(parsed.hostname, "Twilio webhook URL")
    return exact_url


def _group_webhook_parameters(
    params: WebhookParameters,
) -> list[tuple[str, tuple[str, ...]]]:
    if params is None:
        return []

    grouped: dict[str, list[str]] = {}
    get_values = getattr(params, "getall", None)
    if not callable(get_values):
        get_values = getattr(params, "getlist", None)
    if isinstance(params, Mapping):
        for raw_name in params:
            name = _require_parameter_text(raw_name, "parameter name")
            if callable(get_values):
                raw_values: Any = get_values(raw_name)
            else:
                raw_values = params[raw_name]
            values = _coerce_parameter_values(raw_values)
            grouped.setdefault(name, []).extend(values)
    else:
        try:
            pairs = iter(params)
        except TypeError:
            raise TypeError("webhook parameters must be a mapping or pair iterable") from None
        for pair in pairs:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("webhook parameters must contain name/value pairs")
            name = _require_parameter_text(pair[0], "parameter name")
            value = _require_parameter_text(pair[1], "parameter value")
            grouped.setdefault(name, []).append(value)

    return [(name, tuple(sorted(set(grouped[name])))) for name in sorted(grouped)]


def _coerce_parameter_values(raw_values: Any) -> list[str]:
    if isinstance(raw_values, str):
        return [raw_values]
    if isinstance(raw_values, Sequence) and not isinstance(
        raw_values,
        bytes | bytearray,
    ):
        return [_require_parameter_text(value, "parameter value") for value in raw_values]
    raise TypeError("webhook parameter values must be strings or string sequences")


def _require_parameter_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    return value


def _require_text(
    value: str | None,
    label: str,
    *,
    preserve_whitespace: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} is not configured")
    result = value if preserve_whitespace else value.strip()
    if not result:
        raise ValueError(f"{label} is not configured")
    if preserve_whitespace and result != result.strip():
        raise ValueError(f"{label} cannot contain surrounding whitespace")
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


def _validate_hostname(hostname: str, label: str) -> None:
    if not hostname.isascii():
        raise ValueError(f"{label} must contain a valid public hostname")
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
            raise ValueError(f"{label} must contain a valid public hostname") from None


__all__ = [
    "TwilioAPIError",
    "TwilioCallResult",
    "TwilioClient",
    "build_correlation_signature",
    "build_inbound_bridge_twiml",
    "build_outbound_bridge_twiml",
    "build_outbound_voice_event_signature",
    "build_outbound_voice_url",
    "build_status_callback_url",
    "build_status_event_signature",
    "compute_twilio_webhook_signature",
    "normalize_e164",
    "validate_twilio_account_sid",
    "validate_twilio_call_sid",
    "verify_correlation_signature",
    "verify_outbound_voice_event_signature",
    "verify_status_event_signature",
    "verify_twilio_webhook_signature",
]
