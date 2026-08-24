# Codex and Claude integration

Codex and Claude share the same local MCP contract. The voice runtime is a separate OpenAI
Realtime session with bounded application tools; it is not a remote copy of either agent.

## Install both clients

```powershell
uv run agent-hotline install-clients --client all
```

This registers the packaged Codex plugin and a user-scoped Claude MCP server. Start a new
Codex task or Claude session after installation.

Install one client instead:

```powershell
uv run agent-hotline install-clients --client codex
uv run agent-hotline install-clients --client claude
```

The packaged MCP configurations set `HOTLINE_MCP_CLIENT` for audit attribution. Callers do not
need to invent or pass a `source` field, and attribution is never authentication.

The daemon must be running:

```powershell
uv run agent-hotline serve
```

## Automatic Codex permission calls

The Codex plugin bundles a synchronous `PermissionRequest` hook. In a new Codex task, run
`/hooks`, review it, and trust its current definition. When an ordinary Codex task is about to
show a bounded `Bash` or MCP permission prompt, the hook calls the owner and waits for the
structured result.

Only a `resolved` approval from a verified identity whose durable instruction exactly matches the
canonical request returns a one-shot `allow`. A verified denial returns `deny`. An unanswered,
timed-out, failed, malformed, redacted, oversized, changed, or unsupported request returns no hook
decision, so Codex shows its normal local approval prompt. The hook does not grant session or
persistent policy access and does not auto-approve `apply_patch`.

## MCP tools

| Tool | Purpose | Authority |
| --- | --- | --- |
| `contact_human` | Start a call about a material blocker, incident, or decision | Returns a durable event ID |
| `notify_human` | Send a one-way informational call | Cannot approve anything |
| `request_authentication` | Coordinate a legitimate device/browser handoff | Never collects credentials |
| `list_hotline_events` | Read the durable event history | Read-only |
| `get_hotline_result` | Poll one structured result by event ID | Read-only |
| `hotline_status` | Check daemon and transport readiness | Read-only |
| `query_repository_context` | Get bounded status, diff, search, read, or test inventory | Read-only, untrusted evidence |

Use `contact_human` only when work genuinely cannot proceed safely. Send a compact snapshot:
task identity, workspace, branch or state, exact blocker, relevant test result, realistic
options, risks, and the safest default. Redact credentials and irrelevant logs. Retain the
returned event ID, keep the blocked operation paused, and poll `get_hotline_result`.

Continue only from a structured `resolved` result. Treat `no_answer`, `busy`, `failed`,
`timed_out`, `fallback_pending`, `deferred`, an expired result, or vague instructions as no
approval.

`timeout_seconds` is a hard decision deadline, not merely an HTTP wait timeout. When it
elapses, the event becomes expired, the carrier call is terminated, and a later voice response
cannot authorize work. `no_answer_policy=pause` is the default; use `defer` only when the
configured secure fallback should be sent. `notify_only` is valid only for non-blocking
notifications.

## What the voice agent knows

For an outbound escalation, the daemon gives Realtime a bounded sanitized snapshot derived
from the MCP request. For an inbound call, Realtime can list or inspect sanitized Codex task
candidates through application-owned tools.

Realtime does not automatically receive:

- the full Codex or Claude conversation;
- native client tools or MCP connections;
- an unrestricted repository;
- the shell, environment, credentials, or local bearer token;
- arbitrary historical tasks.

If more evidence is needed, the owner must explicitly authorize a bounded repository query.
That makes the call evidence-only.

## Codex capabilities

With the Codex App Server adapter enabled and roots allowlisted, an inbound voice call can:

- list bounded task candidates;
- inspect one exact task;
- prepare and, after full verification, send an instruction;
- interrupt one exact active turn;
- spawn one root task in an allowlisted workspace;
- archive one exact task.

The write operations require `HOTLINE_ALLOW_CODEX_WRITES=true`, exact server readback, a later
owner response, a fresh keypad PIN, and a one-time action grant.

App Server callbacks for exact command execution and turn-scoped permissions can also ask the
owner for a one-turn decision. App Server file-change approval callbacks always decline because
that callback does not provide the complete patch needed for an exact readback.

## Claude capabilities

Claude can call the owner and consume the same structured MCP results. Optional Claude
lifecycle hooks can raise non-authoritative alerts for blocked or denied work.

There is no equivalent deep inbound Claude session-control adapter in this repository. Do not
claim that a phone call can inspect, steer, interrupt, or spawn Claude sessions.

## Actions and runbooks

The Realtime voice agent can list registered actions and exercise the full
prepare/readback/PIN/confirm/execute flow. The built-in runbooks are deterministic mocks only.
No real infrastructure runbooks ship with the project.

Real runbook execution has a separate `HOTLINE_ALLOW_REAL_RUNBOOKS` gate. Keep it off unless a
separately reviewed integration has registered a real executor and verifier.

## Safe local validation

```powershell
uv run agent-hotline demo --auto-decide
```

This runs a fake-call mock and does not validate Codex, Claude, Twilio, OpenAI, PSTN audio, or
DTMF end to end. Use [VERIFICATION.md](VERIFICATION.md) for the live acceptance checklist.

If MCP cannot reach the daemon, report that local dependency failure and leave the operation
paused. Do not claim that the owner was contacted.
