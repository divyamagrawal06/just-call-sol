# Better Call Sol

Better Call Sol is a voice control plane for Codex and Claude. It lets agents phone their owner
about blockers, incidents, approvals, interrupted jobs, or authentication handoffs, while a
FastAPI daemon and SQLite keep decisions and scoped action grants auditable. Live calls use
OpenAI Realtime with Twilio or Vobiz; an authenticated Vapi bridge is available for the hosted
demo path.

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
