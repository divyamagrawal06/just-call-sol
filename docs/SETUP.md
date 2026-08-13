# Production setup

This guide configures the OpenAI Realtime and Twilio production voice path over OpenAI SIP or
direct bidirectional Media Streams. Use Python 3.12 or 3.13 and keep all credentials outside
source control.

## 1. Install

From a checkout:

```powershell
uv sync --python 3.12 --extra dev
uv run agent-hotline init-secrets
uv run agent-hotline install-clients --client all
```

`init-secrets` stores five independent service secrets:

- `HOTLINE_LOCAL_TOKEN`
- `HOTLINE_SIP_CORRELATION_SECRET`
- `HOTLINE_ACTION_SIGNING_SECRET`
- `HOTLINE_FALLBACK_SIGNING_SECRET`
- `HOTLINE_FALLBACK_WEBHOOK_TOKEN`

On Windows they are written to the user environment. On Linux and macOS they are written to
`${XDG_CONFIG_HOME:-~/.config}/agent-hotline/runtime.env` with owner-only permissions, so the
Codex and Claude MCP processes can find them from any repository. Rotate them with
`agent-hotline init-secrets --force`. Rerunning without `--force` preserves existing values;
either mode changes only those five managed keys and preserves provider settings or comments
already present in `runtime.env`.

## 2. Configure the environment

Copy `.env.example` to an ignored `.env` and set:

```dotenv
HOTLINE_ENV=production
HOTLINE_TRANSPORT=openai_realtime
HOTLINE_HOST=127.0.0.1
HOTLINE_PORT=8787
HOTLINE_DAEMON_URL=http://127.0.0.1:8787
PUBLIC_BASE_URL=https://hotline.example.com

OPENAI_API_KEY=
OPENAI_WEBHOOK_SECRET=
OPENAI_PROJECT_ID=
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_VOICE=marin
OPENAI_REALTIME_REASONING_EFFORT=low

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=+12025550124
TWILIO_BRIDGE_MODE=sip

OWNER_PHONE_NUMBER=+12025550123
HOTLINE_OWNER_NAME=Owner
OWNER_CONFIRMATION_PIN=
HOTLINE_ALLOWLISTED_CALLERS=

HOTLINE_WORKSPACE_ROOTS=C:\work\repo-a;C:\work\repo-b
HOTLINE_ALLOW_CODEX_WRITES=false
HOTLINE_ALLOW_REAL_RUNBOOKS=false
HOTLINE_MAX_ACTIVE_CALLS=1
HOTLINE_MAX_CALL_DURATION_SECONDS=1800
HOTLINE_OUTBOUND_RING_TIMEOUT_SECONDS=30
```

Requirements:

- `OPENAI_PROJECT_ID` is the `proj_...` project used for Realtime requests and SIP calls.
- `TWILIO_BRIDGE_MODE` is `sip` (the default) or `media_stream`.
- Both phone numbers use strict E.164 format.
- `OWNER_CONFIRMATION_PIN` contains 6–12 ASCII digits and is never placed in the Realtime
  prompt or agent variables.
- Every Hotline service secret is at least 32 characters and pairwise distinct.
- `HOTLINE_WORKSPACE_ROOTS` contains only explicit Git roots that voice inspection may reach.
- Keep both write gates off until the read-only call path has passed live acceptance.

`HOTLINE_MAX_ACTIVE_CALLS` is enforced during call admission and is intentionally fixed at
`1`. `HOTLINE_MAX_CALL_DURATION_SECONDS` is enforced by the daemon, the outbound Twilio Call,
and each SIP `<Dial>` bridge. Direct media calls remain bounded by daemon-side expiry and the
outbound Call `TimeLimit`.
`HOTLINE_OUTBOUND_RING_TIMEOUT_SECONDS` bounds both owner ringing and SIP bridge setup.

## 3. Configure OpenAI

In the same OpenAI project identified by `OPENAI_PROJECT_ID`:

1. Create or select an API key with access to the configured Realtime model.
2. In `sip` mode, create a webhook pointing to:

   ```text
   https://hotline.example.com/v1/openai/realtime/webhook
   ```

3. In `sip` mode, put the resulting signing secret in `OPENAI_WEBHOOK_SECRET`.
4. In `sip` mode, confirm that the project can receive SIP calls at:

   ```text
   sip:<project-id>@sip.api.openai.com;transport=tls
   ```

In `sip` mode the daemon verifies the exact raw OpenAI webhook body before accepting a call.
In `media_stream` mode no OpenAI incoming-call webhook is used; the daemon opens the
authenticated Realtime WebSocket itself and bridges PCMU audio in both directions.

## 4. Configure Twilio

For the number in `TWILIO_PHONE_NUMBER`, set the incoming voice webhook to:

```text
POST https://hotline.example.com/v1/twilio/voice/incoming
```

The daemon verifies Twilio's request signature, account, destination, direction, and caller
allowlist before returning TwiML. `sip` mode bridges the call to OpenAI SIP;
`media_stream` mode returns a signed, call-bound `<Connect><Stream>` to
`/v1/twilio/media`.

Outbound calls are created by the Twilio REST API with a signed TwiML URL:

```text
POST https://hotline.example.com/v1/twilio/voice/outbound?event_id=<event>&event_sig=<binding>
```

Twilio fetches this route only after allocating the parent CallSid. The daemon verifies the
Twilio form and event binding, atomically attaches that real parent ID to the durable session,
and then returns the selected signed bridge. This also recovers a call whose Calls API response
was lost.

The parent call separately reports lifecycle events to:

```text
POST https://hotline.example.com/v1/twilio/status?event_id=<event>&event_sig=<binding>
```

as their event-bound parent-call status callback. The binding is an HMAC correlation value,
not owner authority. In `sip` mode, inbound TwiML uses the same route without that query as its
Twilio-signed `<Dial action>` target and submits `DialCallStatus`. Neither callback is used as
authority for a decision, and neither claims to represent an independent nested SIP-child
callback.

Outbound Call creation includes exact `TimeLimit` and `Timeout` parameters. SIP bridge TwiML
repeats the same duration and setup limits as carrier-side defense in depth. A direct media
call ends when its authenticated WebSocket closes and is also bounded by durable session expiry.

Twilio trial accounts are not sufficient for this integration. Trial Call API requests cannot
select an arbitrary instruction URL, and trial TwiML strips both `<Stream>` and `<Dial><Sip>`.
Upgrading funds a prepaid usage balance; disable auto-recharge if manual top-ups are preferred.

If a reverse proxy changes scheme, host, port, path, or form data before verification, Twilio
signatures will fail. Preserve the original public URL and request body exactly.

## 5. Publish only required routes

The production proxy should allow:

```text
POST /v1/openai/realtime/webhook
POST /v1/twilio/voice/incoming
POST /v1/twilio/voice/outbound
POST /v1/twilio/status
```

When `TWILIO_BRIDGE_MODE=media_stream`, also allow:

```text
WSS  /v1/twilio/media
```

When missed-call fallback is enabled, also allow:

```text
GET  /fallback
GET  /fallback/assets/fallback.css
GET  /fallback/assets/fallback.js
POST /v1/fallback/open
POST /v1/fallback/decision
```

Block all other routes at the public proxy. In particular, do not publish local escalation,
event, repository-context, health, dashboard, or Codex-control APIs.

For a short development test only:

```powershell
cloudflared tunnel --url http://127.0.0.1:8787
```

The quick tunnel publishes the whole origin and is not a production access-control boundary.
Use a stable HTTPS proxy with an explicit route allowlist for deployment.

## 6. Start and verify

Start the daemon:

```powershell
uv run agent-hotline serve
```

In another terminal:

```powershell
uv run agent-hotline doctor
uv run agent-hotline doctor --live
```

`doctor` reports only presence and readiness metadata. `doctor --live` also checks the local
daemon and configured OpenAI and Twilio APIs. It does not prove that a PSTN call, webhook,
sideband socket, audio turn, or DTMF event works end to end.

Run the offline mock separately:

```powershell
uv run agent-hotline demo --auto-decide
```

This is deterministic local validation, not a live-call result.

## 7. Live acceptance order

1. Call the Twilio number from `OWNER_PHONE_NUMBER`.
2. Confirm the casual Agent Hotline greeting and a natural interruption.
3. Ask it to list and inspect a known Codex task.
4. End the call normally and inspect the durable event.
5. Place an outbound read-only incident call with `agent-hotline call`.
6. Verify exact readback, a later spoken response, keypad PIN entry followed by `#`, and the
   resulting structured decision.
7. Only then consider `HOTLINE_ALLOW_CODEX_WRITES=true` for a scoped Codex task-control test.

Keep `HOTLINE_ALLOW_REAL_RUNBOOKS=false`. No real infrastructure runbooks ship in this
repository, so enabling the gate alone does not create an AWS, database, deployment, or
batch-control capability.

See [VERIFICATION.md](VERIFICATION.md) for the difference between automated, mocked, and live
validation.

## Provider references

- [OpenAI Realtime API with SIP](https://developers.openai.com/api/docs/guides/realtime-sip)
- [OpenAI Realtime server-side controls](https://developers.openai.com/api/docs/guides/realtime-server-controls)
- [OpenAI Realtime voice activity detection](https://developers.openai.com/api/docs/guides/realtime-vad)
- [Twilio Call resource](https://www.twilio.com/docs/voice/api/call-resource)
- [Twilio Media Streams](https://www.twilio.com/docs/voice/media-streams)
- [Twilio trial Voice limits](https://www.twilio.com/docs/usage/trials/try-out-voice)
- [Twilio `<Dial>`](https://www.twilio.com/docs/voice/twiml/dial)
- [Twilio `<Sip>`](https://www.twilio.com/docs/voice/twiml/sip)
