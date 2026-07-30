# Agent Hotline

Agent Hotline gives Codex and Claude a real phone line to their owner. An agent can call about
a blocker, incident, approval, interrupted job, or authentication handoff; the owner can call
back to inspect Codex tasks and authorize a narrowly scoped task action.

The production voice path is OpenAI Realtime over SIP, with Twilio carrying the PSTN leg.
FastAPI and SQLite keep policy, correlation, audit state, decisions, and one-time action grants
under application control.

## What ships

- A persistent `agent-hotline` daemon and SQLite event store.
- A shared stdio MCP server for Codex and Claude.
- A conversational OpenAI Realtime voice agent with interruption and silence handling.
- Inbound and outbound Twilio phone flows.
- Bounded, sanitized Codex task and repository inspection.
- Confirmed Codex task controls: instruct, interrupt, spawn a root task, and archive.
- Strict mock runbooks that exercise the full authorization flow without touching real
  infrastructure.
- An optional PIN-gated missed-call fallback.

OpenAI Realtime receives only the bounded context and function tools supplied for that call. It
does not inherit a Codex or Claude process, native tool access, complete repository context,
shell access, credentials, or unrestricted filesystem access.

## Safety model

Voice is an interface, not an authority boundary.

- No answer, silence, voicemail, an ambiguous reply, or provider success is approval.
- Every live-call decision uses a server-generated exact readback, a later owner response, and
  a fresh keypad PIN verified outside the model.
- PIN digits are transient server state. They are not sent to the model, tool arguments,
  transcript, or database.
- Inbound callers must pass Twilio signature checks and the configured E.164 allowlist.
- Signed SIP correlation binds the carrier leg to the accepted Realtime call.
- Action grants are exact, expiring, and one-time. A changed target or state requires a new
  confirmation.
- Repository evidence is bounded and untrusted. Exposing it makes that call evidence-only.
- Spoken free text never becomes a shell command.
- Codex writes and real runbook execution have separate, default-off gates.

The built-in runbooks are mocks. This repository does not ship production AWS, database,
deployment, or batch-job executors. Codex file-change approval callbacks always decline because
the current callback does not contain a complete patch that can be read back exactly.

## Quick start

Agent Hotline supports Python 3.12 and 3.13. From a source checkout:

```powershell
uv sync --python 3.12 --extra dev
uv run agent-hotline init-secrets
uv run pytest -q
uv run agent-hotline demo --auto-decide
uv run agent-hotline install-clients --client all
```

`demo --auto-decide` is an offline mock. It forces the fake transport, keeps both write gates
off, places no phone call, and changes no real resource.

Copy `.env.example` to an ignored `.env`, then supply your own OpenAI, Twilio, phone, PIN, and
public-origin configuration. The core live values are:

```dotenv
OPENAI_API_KEY=
OPENAI_WEBHOOK_SECRET=
OPENAI_PROJECT_ID=

TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=

OWNER_PHONE_NUMBER=
OWNER_CONFIRMATION_PIN=
HOTLINE_OWNER_NAME=Owner
PUBLIC_BASE_URL=https://hotline.example.com
```

`agent-hotline init-secrets` creates five independent service secrets without printing their
values. It preserves them on ordinary reruns and rotates only those keys with `--force`.
Keep the generated values out of source control.

Start the daemon and run the credential-aware checks:

```powershell
uv run agent-hotline serve
uv run agent-hotline doctor --live
```

Then place a real configured-provider call:

```powershell
uv run agent-hotline call `
  --kind incident `
  --severity high `
  --summary "The training run stopped after its compute instance disappeared." `
  --question "Retry once with the same configuration, or keep it paused?"
```

Add `--no-wait` to return as soon as call origination finishes without changing the durable
decision deadline. Keep the returned event ID and retrieve it later:

```powershell
uv run agent-hotline result evt_... --watch
```

## Public route contract

OpenAI and Twilio need these four provider webhook routes:

```text
POST /v1/openai/realtime/webhook
POST /v1/twilio/voice/incoming
POST /v1/twilio/voice/outbound
POST /v1/twilio/status
```

If missed-call fallback is enabled, its page, static assets, and two `/v1/fallback/*` endpoints
also need to be reachable by the owner. Local escalation, event, repository, dashboard, health,
and Codex-control surfaces must not be exposed through the production proxy.

A quick tunnel such as the following is useful only for short-lived development:

```powershell
cloudflared tunnel --url http://127.0.0.1:8787
```

That command exposes the whole app origin. For production, put the daemon behind a stable HTTPS
reverse proxy that allowlists only the required provider routes and optional fallback routes,
enforces request-size and rate limits, and keeps local APIs private.

## Install from a wheel

```powershell
uv build --wheel
uv tool install .\dist\agent_hotline-0.2.0-py3-none-any.whl
agent-hotline install-clients --client all
```

The wheel includes the CLI, MCP executables, Codex plugin, skill, policy, and agent metadata.

## Documentation

- [Production setup](docs/SETUP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Security model](docs/SECURITY.md)
- [Codex and Claude integration](docs/CODEX_CLAUDE.md)
- [Missed-call fallback](docs/FALLBACK.md)
- [Demo and acceptance walkthrough](docs/DEMO.md)
- [Verification status](docs/VERIFICATION.md)

The retired Sarvam implementation is preserved only on branch `agent/sarvam-event-gate-tools`.
