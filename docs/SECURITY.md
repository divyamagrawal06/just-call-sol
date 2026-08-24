# Security model

Agent Hotline assumes that audio, caller ID, model output, provider events, repository text,
and agent-produced context can all be wrong or adversarial. The daemon is the authority
boundary.

## Core invariants

1. No answer, voicemail, silence, background speech, failure, or ambiguity is approval.
2. A live provider session is necessary but not sufficient for authority.
3. Every live-call decision is bound to one event and a fresh verification window.
4. Every action grant is exact, expiring, and one-time.
5. Free-form speech never becomes a shell command.
6. Secrets and PIN digits are never exposed to the voice model.

## Secret separation

Production uses independent values for:

- local daemon authentication: `HOTLINE_LOCAL_TOKEN`;
- SIP correlation: `HOTLINE_SIP_CORRELATION_SECRET`;
- decision and action capability signing: `HOTLINE_ACTION_SIGNING_SECRET`;
- fallback link signing: `HOTLINE_FALLBACK_SIGNING_SECRET`;
- fallback delivery authentication: `HOTLINE_FALLBACK_WEBHOOK_TOKEN`;
- OpenAI webhook verification: `OPENAI_WEBHOOK_SECRET`;
- Twilio request verification: `TWILIO_AUTH_TOKEN`;
- Vobiz REST authentication and provider-defined callback signing: `VOBIZ_AUTH_TOKEN`;
- Vapi function-bridge authentication: `VAPI_WEBHOOK_TOKEN`;
- owner verification: `OWNER_CONFIRMATION_PIN`.

The six Hotline-managed service secrets must be at least 32 characters and pairwise distinct.
Do not reuse an API key, webhook secret, auth token, or PIN for another role. Vobiz's documented
callback scheme intentionally uses its account auth token as the signing key; this is a provider
contract, not a reusable Hotline service secret.

## Public ingress

The production proxy allowlists only:

```text
POST /v1/openai/realtime/webhook
POST /v1/twilio/voice/incoming
POST /v1/twilio/voice/outbound
POST /v1/twilio/status
```

or, for the Vobiz carrier profile, the OpenAI route plus:

```text
POST /v1/vobiz/voice/incoming
POST /v1/vobiz/voice/outbound
POST /v1/vobiz/ring
POST /v1/vobiz/hangup
```

The optional Vapi bridge adds only `POST /v1/vapi/webhook`.

When enabled, the fallback page, assets, and two fallback API routes are also public. Local escalation,
event, repository, dashboard, health, database, MCP, and Codex-control surfaces stay private.

OpenAI webhooks are verified against the exact raw body. Twilio form requests are verified
against the original public URL and body. Every Vobiz form requires a base64-encoded
HMAC-SHA256 signed with `VOBIZ_AUTH_TOKEN`: V3 signs `baseURL + "." + nonce`, while the V2
fallback signs `baseURL + nonce`. The daemon prefers the
`X-Vobiz-Signature-V3`/`X-Vobiz-Signature-V3-Nonce` pair, accepts the corresponding V2 pair,
requires the provider's 20-digit nonce, compares signatures in constant time, and durably
tracks verified nonces against replay. Query parameters are excluded from the provider HMAC,
so the outbound answer, ring, and hangup routes additionally require separate domain-specific
event bindings. Vapi requires a distinct bearer token, exact assistant and phone-number IDs,
and an owner or explicitly allowlisted customer number. Provider handlers also apply bounded
body sizes, rate limits, idempotency, and correlation checks. Put another request-size and
rate-limit layer at the reverse proxy.

A raw quick tunnel exposes the entire app origin and is suitable only for temporary
development.

The daemon and carrier independently cap call lifetime. Outbound carrier Call creation sets a
maximum duration and ring timeout, and both inbound and outbound SIP `<Dial>` bridges set
matching `timeLimit` and setup `timeout` values. Direct media sessions are bounded by daemon
expiry, and closing either authenticated WebSocket tears down the audio path. Daemon maintenance
also expires stale local sessions so a missing status callback cannot permanently consume the
single active-call slot.

## Inbound identity

The incoming carrier route requires all of the following:

- valid Twilio signature or recent Vobiz HMAC;
- expected carrier account;
- expected destination number;
- inbound direction;
- caller number matching the owner or explicit E.164 allowlist.

In SIP mode, the returned TwiML or Vobiz XML adds signed, short-lived correlation data to the
OpenAI leg and the signed incoming-call webhook must match it. In direct media mode, an
expiring, one-time carrier admission is bound to the authenticated Twilio stream before the
inbound event exists. Direct SIP, forged streams, replayed correlation, expired admission, or
an allowlist mismatch is rejected.

The allowlist grants access to a conversation and bounded read-only task discovery. It does
not grant a decision, repository read, or write.

## Exact confirmation

The server, not the model, creates the authoritative readback. The Realtime controller binds a
completed spoken response transcript to the expected text, waits for either OpenAI's SIP output
buffer or a Twilio media mark to report that playback fully drained, and then observes a later
owner speech turn before it will arm keypad verification.

The owner enters the PIN by DTMF followed by `#`. Digits live only in the active controller,
are compared in constant time, and are never passed to the model or persisted. Failed attempts
are counted across every decision, action, and repository window in the call. Reaching the
limit permanently locks verification for that call; preparing a new scope cannot reset it.
Interruption, correction, or expiry requires a fresh readback and PIN window.

## Decisions and actions

A live-call decision cannot be recorded until the exact readback and fresh PIN flow completes.
A changed instruction or constraint requires a new preparation.

Medium- and high-risk actions require an explicit owner response after the immutable scope
readback plus the fresh server-side DTMF PIN. The model must return the server phrase unchanged
as a scope-integrity check, but that model-supplied text is not a separate identity factor. The
resulting grant binds the action type, resource, environment, parameters, workspace and task,
server-derived state fingerprint when applicable, expiry, and use count. Execution consumes it
once and records a durable succeeded, failed, or unknown outcome; unknown work is not retried
automatically.

Two independent default-off gates limit side effects:

- `HOTLINE_ALLOW_CODEX_WRITES` controls voice-initiated Codex task writes.
- `HOTLINE_ALLOW_REAL_RUNBOOKS` controls registered real runbook executors.

`HOTLINE_DEMO_AUTO_EXECUTE_ACTIONS` is a separate development-only shortcut that skips spoken
PIN verification for an otherwise prepared and allowlisted Codex action. Configuration rejects
it when `HOTLINE_ENV=production`; do not expose a demo daemon as a production control surface.

The default registry contains only mock runbooks. No production cloud, database, deployment,
or batch executor ships here. Enabling the real-runbook gate does not invent one.

Codex command and permission callbacks are limited to exact one-turn responses. The synchronous
Codex `PermissionRequest` hook returns `allow` only after a verified, resolved approval whose
durable instruction exactly equals the canonical bounded request; all mismatch and failure paths
return no decision and preserve the local prompt. Verified denial can return `deny`. App Server
file-change callbacks always decline because that callback omits the patch, and the hook abstains
on `apply_patch` because a lossless patch readback is not practical over PSTN.

## Repository and agent context

The Realtime model does not receive the agent's native context. It receives a bounded,
sanitized snapshot and tool results selected by the daemon.

Repository queries are restricted to explicit Git roots, trusted Git executable resolution,
bounded operations, capped output, and credential-path denials. Repository text is evidence,
not instruction. Static test inventory is not proof that tests ran.

Voice repository access requires a separate warning, consent, and PIN. The event becomes
evidence-only as soon as the authenticated query is accepted, even if the query later fails.

## Authentication handoffs

Never ask the owner to speak a password, OTP, MFA code, recovery code, private key, API key, or
cloud credential. The phone call may explain why authentication is needed and direct the owner
to a legitimate device or browser flow. The actual secret stays in that flow.

## Restart and failure behavior

Realtime exposes no cursor for replaying sideband events. If the daemon restarts or the
sideband WebSocket loses continuity, Hotline terminates both provider legs and records failure.
Each provider leg has its own durable termination job. A retry deadline is committed before
provider I/O, so a timeout, crash, or ambiguous response remains pending for startup and
background reconciliation rather than being mistaken for a completed hangup. Hotline never
replays an ambiguous tool output or presents the call as recovered.

MCP receives the durable event ID when the call starts and can poll it again after restart.
Provider completion, carrier status, or a later fallback result can reconcile that event but
cannot retroactively turn transport success into approval. Every live conversation also has a
server-enforced overall duration cap, including inbound calls and long pauses.

Failures are sanitized before reaching audio or logs. Never log raw request bodies, provider
authorization headers, phone numbers, service secrets, PINs, or unrestricted transcripts.

## Deployment checklist

- Bind the daemon to loopback behind a route-allowlisting HTTPS proxy.
- Keep `.env`, `.hotline/`, logs, databases, and provider payload captures out of source
  control.
- Use distinct production credentials and rotate them after exposure.
- Start with one active call and both write gates off.
- Verify inbound and outbound calls, interruption, silence, exact readback, DTMF isolation,
  denial, timeout, and fail-closed restart termination before enabling writes.
- Audit allowlisted phone numbers and Git roots.
- Keep real infrastructure executors in a separately reviewed integration package.
