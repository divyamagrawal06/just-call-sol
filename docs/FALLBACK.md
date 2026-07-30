# Missed-call fallback

The optional fallback gives the owner a short-lived way to respond when a blocking call whose
`no_answer_policy` is `defer` ends without a decision. The default `pause` policy does not send
a fallback. This is a recovery channel, not a shortcut around voice verification.

## Configure

```dotenv
HOTLINE_FALLBACK_WEBHOOK_URL=https://owner-controlled.example/notify
HOTLINE_FALLBACK_WEBHOOK_TOKEN=<independent 32+ character delivery token>
HOTLINE_FALLBACK_SIGNING_SECRET=<independent 32+ character signing secret>
HOTLINE_FALLBACK_TTL_SECONDS=900
HOTLINE_FALLBACK_MAX_PIN_ATTEMPTS=5
OWNER_CONFIRMATION_PIN=<6-12 digits>
PUBLIC_BASE_URL=https://hotline.example.com
```

The delivery endpoint must be owner-controlled. The daemon sends a generic notification and a
one-time URL. It does not send task context, a PIN, credentials, or authorization.

## Public routes

When fallback is enabled, the production proxy additionally allows:

```text
GET  /fallback
GET  /fallback/assets/fallback.css
GET  /fallback/assets/fallback.js
POST /v1/fallback/open
POST /v1/fallback/decision
```

Keep local daemon APIs blocked.

## Flow

1. A blocking call configured with `no_answer_policy=defer` reaches a terminal no-decision
   state.
2. The daemon persists a fallback record and sends a generic notification.
3. The bearer token stays in the URL fragment, so it is not sent in the initial HTTP request.
4. The page submits the token and owner PIN to `/v1/fallback/open`.
5. After verification, the server returns bounded context and a separate one-time submission
   token.
6. The owner records approve, deny, defer, or an instruction through
   `/v1/fallback/decision`.
7. The token is consumed and the durable event is reconciled.

The page uses no third-party script, font, analytics, or asset origin. Responses are no-store
and include restrictive browser security headers.

## Authority limits

A fallback response can resolve the pending decision shown on that page. It cannot:

- confirm or execute a registered action;
- authorize a Codex task write;
- broaden the pending scope;
- run a shell command;
- reveal repository evidence;
- approve a different event.

If an action is still required, start a fresh verified voice call and perform the normal exact
readback and PIN flow.

## Restart behavior

Fallback records, expiry, attempt count, and decisions survive daemon restart. MCP callers
retain the event ID returned when the call starts and can poll it again. A blocking CLI/HTTP
waiter's process-local socket does not survive restart and must not be represented as an
uninterrupted waiter.

Expired, consumed, locked, or mismatched links fail closed. Generic error messages avoid
revealing whether an event or token exists.
