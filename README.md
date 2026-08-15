# Better Call Sol

**Your coding agent can reach you before a blocker turns into wasted time.**

Better Call Sol gives Codex and Claude a real phone line to their owner. An agent can call with
a compact incident brief when it needs a decision, approval, authentication handoff, or help
recovering interrupted work. You can call back to inspect, steer, interrupt, or start bounded
Codex tasks without handing a voice model unrestricted access to your machine.

OpenAI Realtime provides the low-latency conversation: natural speech, interruption handling,
turn-taking, and tool calls. Codex remains the coding runtime. A local FastAPI control plane
connects the two, enforces policy, and records every event, decision, and one-time action grant
in SQLite. Twilio or Vobiz carries the phone leg; Vapi can power a hosted demo path.

## How Codex and OpenAI Realtime work together

```text
Codex or Claude -> local Hotline daemon -> Twilio/Vobiz -> OpenAI Realtime -> owner phone
                         ^                      |
                         +---- bounded tools ---+
```

1. **Codex raises the right signal.** The bundled MCP/plugin integration sends a sanitized,
   structured brief to the local daemon instead of exposing the whole repository or terminal.
2. **OpenAI Realtime runs the conversation.** It speaks and listens in real time, then calls only
   the narrow tools supplied for that call. It never inherits Codex shell access, credentials,
   or unrestricted filesystem access.
3. **The daemon talks to Codex.** Tool calls return to the local control plane, which can list or
   inspect tasks and—when policy allows—instruct, interrupt, spawn, or archive a specific Codex
   task through the Codex app-server boundary.
4. **Authority stays local.** Sensitive actions use exact readback, server-side confirmation,
   short-lived scoped grants, and durable audit receipts. Silence, voicemail, or a plausible
   model response is never treated as approval.

The result is a practical human-in-the-loop control surface for long-running agent work: fast
enough to use from your pocket, narrow enough to trust, and auditable enough to operate.

## Setup and run

Requires Python 3.12 or 3.13 and [`uv`](https://docs.astral.sh/uv/).

```powershell
uv sync --python 3.12 --extra dev
uv run agent-hotline init-secrets
Copy-Item .env.example .env
```

Fill `.env` with your OpenAI, carrier, phone, confirmation PIN, and public URL settings, then
start the daemon:

```powershell
uv run agent-hotline serve
```

For a credential-free local check, run `uv run agent-hotline demo --auto-decide`. The demo uses
fake transport, places no phone call, and changes no real resource.

## Usage example

With the daemon and a carrier configured, place an incident call:

```powershell
uv run agent-hotline call `
  --kind incident `
  --severity high `
  --summary "A training run stopped unexpectedly." `
  --question "Retry once, or keep it paused?"
```

See [production setup](docs/SETUP.md), [security](docs/SECURITY.md), and the
[demo walkthrough](docs/DEMO.md) for detailed configuration and verification.
