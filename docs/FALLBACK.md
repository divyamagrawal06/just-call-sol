# Missed-call secure fallback

Agent Hotline can send a one-time decision link when a blocking outbound call ends without
an authoritative decision. Delivery is provider-neutral: the daemon calls an
owner-controlled HTTPS webhook, and that bridge may send an SMS, push notification, or
another private notification.

## Configuration

```text
PUBLIC_BASE_URL=https://<stable-public-origin>
HOTLINE_FALLBACK_WEBHOOK_URL=https://<owner-controlled-bridge>/agent-hotline
HOTLINE_FALLBACK_WEBHOOK_TOKEN=<independent-random-bearer>
HOTLINE_FALLBACK_TTL_SECONDS=900
HOTLINE_FALLBACK_MAX_PIN_ATTEMPTS=5
```

The webhook URL must use HTTPS. Loopback HTTP is accepted for local development. The
webhook token must be independent of the local, voice-tool, callback, provider, and owner
credentials.

## Delivery contract

The daemon sends:

```http
POST <HOTLINE_FALLBACK_WEBHOOK_URL>
Authorization: Bearer <HOTLINE_FALLBACK_WEBHOOK_TOKEN>
Idempotency-Key: <fallback_id>
Content-Type: application/json
```

```json
{
  "type": "agent_hotline.missed_call",
  "version": 1,
  "event_id": "evt_opaque",
  "title": "Agent Hotline needs your decision",
  "message": "A call from Agent Hotline was missed. Open the secure one-time link to review and respond.",
  "url": "https://<PUBLIC_BASE_URL>/fallback#<one-time-capability>",
  "expires_at": "2026-07-26T12:00:00Z"
}
```

The notification deliberately contains no task summary, question, workspace, thread name,
phone number, transcript, or credential. Treat the URL as a short-lived bearer and keep
notification previews private.

## Browser flow

1. The token stays in the URL fragment, so it is not sent in the page request or ordinary
   proxy access logs.
2. The page immediately removes the fragment from browser history.
3. The owner enters the configured PIN before the daemon reveals any event context.
4. The daemon caps failed PIN attempts and issues a shorter-lived submission capability.
5. The page displays the exact pending request and requires an explicit confirmation.
6. The daemon atomically records one decision and consumes the durable fallback record.
7. Replays, expired links, state drift, prior decisions, and repository-evidence events fail
   closed.

The fallback may approve, deny, defer, record an instruction, or acknowledge completion of
a legitimate authentication handoff. It cannot confirm or execute a registered action and
never returns an action grant.

## Failure behavior

- If delivery fails, the original no-answer/busy/failure result remains non-authoritative.
- If the link expires or reaches its PIN-attempt limit, the event fails and the waiting
  agent receives no approval.
- If the daemon restarts, link state and one-time consumption remain in SQLite.
- A duplicate provider callback does not send a second notification.
- Rotating the Hotline signing secret invalidates outstanding links.

The current delivery adapter is a generic webhook. Integrate SMS or push at the bridge,
rather than placing vendor credentials or recipient phone numbers in Agent Hotline.
