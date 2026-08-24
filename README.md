# Better Call Sol

**Give your coding agent a phone line.**

Better Call Sol lets Codex or Claude call you when work needs a human decision: approve a
recovery, finish an authentication handoff, respond to an incident, or decide what an agent
should do next. You can also call back to inspect and control bounded Codex tasks from your
phone.

[Watch the two-minute demo](https://youtu.be/5xuRL5zwR4c)

## What it does

- Places natural, interruptible calls with OpenAI Realtime.
- Automatically calls when Codex reaches a bounded shell or MCP approval and can return the
  verified decision to that same task.
- Lets an owner call back to list, inspect, instruct, interrupt, spawn, or archive Codex tasks.
- Works with **Vobiz** or **Twilio** for the phone connection.
- Exposes only explicit Git repositories and narrowly scoped task actions.
- Stores events, decisions, grants, and call state in a local SQLite database.
- Ships as a Codex plugin, an MCP server, and an optional Claude Code integration.

```text
Codex / Claude
      |
      v
Better Call Sol daemon <----> OpenAI Realtime
      |                              |
      +-------- Vobiz / Twilio ------+
                     |
                  your phone
```

The voice model never receives unrestricted shell, credential, or filesystem access. All Codex
operations return through the local daemon, where workspace allowlists and action policy are
enforced.

## Install

### Requirements

- Windows, macOS, or Linux
- Python 3.12 or 3.13
- [uv](https://docs.astral.sh/uv/)
- Git
- Codex desktop or CLI, signed in
- An OpenAI Platform project with API billing and access to Realtime
- A Vobiz or paid Twilio account with a voice-capable phone number
- A public HTTPS URL for carrier and OpenAI webhooks

Claude Code is optional. A ChatGPT or Codex subscription does not cover OpenAI API usage.

### 1. Clone and install

```powershell
git clone https://github.com/divyamagrawal06/better-call-sol.git
cd better-call-sol
uv sync --python 3.12
Copy-Item .env.example .env
uv run agent-hotline init-secrets
```

On macOS or Linux, replace the copy command with:

```bash
cp .env.example .env
```

`init-secrets` generates the local signing and service secrets without printing them. Existing
secrets are preserved unless you explicitly run it with `--force`.

### 2. Configure OpenAI and the owner

Open `.env` and fill the common settings:

```dotenv
HOTLINE_ENV=production
HOTLINE_TRANSPORT=openai_realtime
PUBLIC_BASE_URL=https://hotline.example.com

OPENAI_API_KEY=
OPENAI_PROJECT_ID=
OPENAI_WEBHOOK_SECRET=
OPENAI_REALTIME_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_VOICE=marin

OWNER_PHONE_NUMBER=+12025550123
OWNER_CONFIRMATION_PIN=
HOTLINE_WORKSPACE_ROOTS=C:\code\repo-a;C:\code\repo-b
```

Use E.164 phone numbers, such as `+919876543210`. The confirmation PIN must contain 6–12
digits. `HOTLINE_WORKSPACE_ROOTS` must contain explicit Git repository roots; Better Call Sol
will not search the rest of the machine.

For Vobiz or the default Twilio SIP mode, create an OpenAI webhook in the same project as
`OPENAI_PROJECT_ID` and subscribe it to `realtime.call.incoming`:

```text
POST https://hotline.example.com/v1/openai/realtime/webhook
```

Store its signing secret as `OPENAI_WEBHOOK_SECRET`.

### 3. Choose a carrier

You need **one** of the following configurations.

#### Option A: Vobiz

Set:

```dotenv
HOTLINE_CARRIER=vobiz
VOBIZ_AUTH_ID=
VOBIZ_AUTH_TOKEN=
VOBIZ_PHONE_NUMBER=+12025550124
```

Attach a Vobiz XML application to that number with these URLs:

```text
Answer URL:  POST https://hotline.example.com/v1/vobiz/voice/incoming
Hangup URL:  POST https://hotline.example.com/v1/vobiz/hangup
```

Vobiz carries the phone call and connects it to the OpenAI Realtime SIP endpoint. Better Call
Sol verifies Vobiz callback signatures before accepting or changing call state.

#### Option B: Twilio

Better Call Sol supports OpenAI SIP and Twilio bidirectional Media Streams. SIP is the default:

```dotenv
HOTLINE_CARRIER=twilio
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=+12025550124
TWILIO_BRIDGE_MODE=sip
```

Set the number's incoming Voice webhook to:

```text
POST https://hotline.example.com/v1/twilio/voice/incoming
```

Twilio trial accounts block the `<Dial><Sip>` and `<Stream>` paths used by this project, so a
paid account is required for live calls. Set `TWILIO_BRIDGE_MODE=media_stream` only if you want
the daemon to bridge audio directly instead of using OpenAI SIP.

### 4. Install the Codex plugin

For Codex:

```powershell
uv run agent-hotline install-clients --client codex
```

For Codex and Claude Code:

```powershell
uv run agent-hotline install-clients --client all
```

This installs the command suite, registers this checkout as a Codex marketplace, installs the
bundled `agent-hotline` plugin, and optionally registers the Claude MCP server. Start a **new
Codex task** after installation so the plugin is loaded. In that new task, run `/hooks`, review
the Agent Hotline `PermissionRequest` hook, and trust it. Codex deliberately skips new or changed
command hooks until you do this once.

With the daemon running, a normal Codex task can then call you automatically when a bounded
`Bash` or MCP permission prompt is about to appear. A verified, exact approval grants only that
one invocation; an explicit denial denies it. No answer, timeout, provider failure, changed scope,
or an unavailable daemon falls back to the ordinary local Codex approval prompt. File patches
also stay on the local prompt because reading a patch losslessly over a phone call is not a useful
authorization flow.

### 5. Run it

Start the daemon:

```powershell
uv run agent-hotline serve
```

Run the readiness checks in another terminal:

```powershell
uv run agent-hotline doctor
uv run agent-hotline doctor --live
```

For a short local test, [Cloudflare Tunnel][cloudflare-quick-tunnel] can provide a temporary
HTTPS URL:

```powershell
cloudflared tunnel --url http://127.0.0.1:8787
```

Put the generated origin in `PUBLIC_BASE_URL`, update the provider webhooks, and restart the
daemon. Quick Tunnels change URL when restarted and expose the whole local origin, so use a
stable HTTPS reverse proxy with a route allowlist for an always-on deployment.

## Place a call

```powershell
uv run agent-hotline call `
  --kind incident `
  --severity high `
  --summary "A training run stopped unexpectedly." `
  --question "Retry once, or keep it paused?"
```

For a credential-free local check:

```powershell
uv run agent-hotline demo --auto-decide
```

The local demo uses a fake carrier, places no call, and changes no real resource.

## Safe defaults

Codex mutations are disabled by default. First verify inbound calling, task listing, task
inspection, normal hangup, and an outbound decision call. Only then enable scoped task writes:

```dotenv
HOTLINE_ALLOW_CODEX_WRITES=true
```

Keep `HOTLINE_ALLOW_REAL_RUNBOOKS=false`. This repository does not ship production AWS,
database, deployment, or batch-control executors.

## Documentation

- [Production setup](docs/SETUP.md)
- [Security model](docs/SECURITY.md)
- [Demo walkthrough](docs/DEMO.md)
- [Verification matrix](docs/VERIFICATION.md)
- [OpenAI Realtime SIP](https://developers.openai.com/api/docs/guides/realtime-sip)
- [Twilio Voice trial restrictions](https://www.twilio.com/docs/usage/trials/try-out-voice)
- [Vobiz OpenAI Realtime integration](https://www.vobiz.ai/docs/integrations/openai-realtime)

## License

[MIT](LICENSE)

[cloudflare-quick-tunnel]: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/
