"""Provider-neutral signatures used to bind carrier legs to durable call events."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Literal

CorrelationDirection = Literal["inbound", "outbound"]
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{1,299}$")
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")


def build_correlation_signature(
    secret: str,
    *,
    direction: CorrelationDirection,
    call_sid: str,
    event_id: str | None = None,
    caller_phone: str | None = None,
    admission_nonce: str | None = None,
    expires_at_epoch: int | None = None,
) -> str:
    """Sign immutable carrier/SIP correlation fields without exposing the secret."""

    if len(secret) < 16:
        raise ValueError("correlation signing secret must contain at least 16 characters")
    if direction not in {"inbound", "outbound"}:
        raise ValueError("unsupported correlation direction")
    if _SAFE_ID.fullmatch(call_sid) is None:
        raise ValueError("call_sid is not a safe identifier")
    if event_id is not None and _SAFE_ID.fullmatch(event_id) is None:
        raise ValueError("event_id is not a safe identifier")
    if direction == "outbound" and event_id is None:
        raise ValueError("outbound correlation requires event_id")
    if direction == "outbound" and (
        caller_phone is not None or admission_nonce is not None or expires_at_epoch is not None
    ):
        raise ValueError("outbound correlation cannot include inbound admission fields")
    if direction == "inbound" and (
        event_id is not None
        or caller_phone is None
        or admission_nonce is None
        or expires_at_epoch is None
    ):
        raise ValueError(
            "inbound correlation requires a caller, nonce, and expiry without an event"
        )
    if caller_phone is not None and _E164.fullmatch(caller_phone) is None:
        raise ValueError("caller_phone must be strict E.164")
    if admission_nonce is not None and _SAFE_ID.fullmatch(admission_nonce) is None:
        raise ValueError("admission_nonce is not a safe identifier")
    if expires_at_epoch is not None and not 1 <= expires_at_epoch <= 9_999_999_999:
        raise ValueError("expires_at_epoch is invalid")
    payload = (
        f"v3\n{direction}\n{call_sid}\n{event_id or ''}\n{caller_phone or ''}\n"
        f"{admission_nonce or ''}\n{expires_at_epoch or ''}"
    ).encode()
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_correlation_signature(
    secret: str,
    *,
    direction: CorrelationDirection,
    call_sid: str,
    event_id: str | None,
    caller_phone: str | None = None,
    admission_nonce: str | None = None,
    expires_at_epoch: int | None = None,
    signature: str,
) -> bool:
    """Return true only for a canonical signature over the exact correlation."""

    try:
        expected = build_correlation_signature(
            secret,
            direction=direction,
            call_sid=call_sid,
            event_id=event_id,
            caller_phone=caller_phone,
            admission_nonce=admission_nonce,
            expires_at_epoch=expires_at_epoch,
        )
    except ValueError:
        return False
    return bool(signature) and hmac.compare_digest(expected, signature)


__all__ = [
    "CorrelationDirection",
    "build_correlation_signature",
    "verify_correlation_signature",
]
