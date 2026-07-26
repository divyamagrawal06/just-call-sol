# Codex and Claude integration

Agent Hotline presents one stdio MCP contract to both clients. Codex also has a deeper,
independent App Server adapter for task enumeration and control.

## Shared MCP tools

| Tool | Purpose | Blocking |
| --- | --- | --- |
| `contact_human` | Discuss a meaningful blocker and return a structured decision | Yes |
| `notify_human` | Send an informational call without waiting | No |
| `request_authentication` | Request a legitimate device/browser sign-in handoff | Yes |
| `list_hotline_events` | Read recent event states | No |
| `hotline_status` | Read daemon/provider readiness | No |
| `query_repository_context` | Read bounded evidence from an allowlisted repository | No |

The MCP subprocess contains no provider credential. It calls the loopback daemon with
`HOTLINE_LOCAL_TOKEN`.

`query_repository_context` supports only `status`, `diff`, `search`, `read`, and `tests`.
Search is literal, reads are repository-relative and capped at 80 lines, and `tests` is a
static inventory rather than a test runner. The daemon rejects roots outside
`HOTLINE_WORKSPACE_ROOTS`, path/symlink escapes, binary or oversized files, credential files,
and generated/vendor trees. Returned source remains untrusted evidence.

## Install both clients

First install the project:

```powershell
uv sync --python 3.12 --extra dev
uv run agent-hotline install-clients --client all
```

Verify:

```powershell
codex mcp get agent_hotline
claude mcp get agent-hotline
```

Restart Codex/Claude sessions after installation.

`install-clients` is idempotent for matching registrations. It inspects the editable uv tool,
Codex marketplace/plugin, and Claude user-scoped MCP entry before changing anything. A
same-name entry that points elsewhere is reported as a conflict and is never silently replaced.
On Windows, running the command from the Agent Hotline uv tool skips self-reinstallation so uv
does not try to remove the environment containing the active process.

Manual fallback:

```powershell
uv tool install --editable .
codex mcp add agent_hotline -- agent-hotline-mcp
claude mcp add --scope user agent-hotline -- agent-hotline-mcp
```

The repository’s Codex plugin also provides the operational skill and safety policy under
`plugins/agent-hotline/`.

## Codex behavior

Call `contact_human` only when work genuinely needs a human decision, authentication
handoff, or urgent incident response. Before calling, supply:

- exact task and workspace reference;
- branch/commit/dirty state when relevant;
- compact diff and test summaries;
- exact last error;
- pending decision and realistic options;
- durable constraints;
- a stable dedupe key.

Continue only when the result is `resolved`. Apply `instruction` and every `constraint`
literally. Approved action references authorize only their exact scope and expiry.

Codex `0.144.6` deep control runs as a supervised direct child:

```text
codex app-server --stdio
```

The client performs `initialize`/`initialized` and permits only:

```text
thread/list
thread/read
thread/start
thread/resume
thread/archive
turn/start
turn/steer
turn/interrupt
```

It intentionally exposes no shell or arbitrary command method.

Inbound phrases map conservatively:

| Caller intent | App Server behavior |
| --- | --- |
| “Check task X” | `thread/list`, disambiguate, `thread/read` |
| “Tell X to use the safer migration” | `turn/steer` if active, otherwise `turn/start` |
| “Pause X” | Resolve exact active turn, then `turn/interrupt` |
| “Start a root task in repo Y” | Validate workspace root, `thread/start`, then `turn/start` |
| “Archive X” | Resolve exact task and `thread/archive` |
| “Terminate batch runs” | No App Server command; use a registered runbook |

The minimum watcher consumes `turn/started` and `turn/completed`. A registered handler can
answer the installed version’s `item/commandExecution/requestApproval`. Unrecognized
server-initiated requests fail closed.

## Claude behavior

Claude Code `2.1.211` uses the same MCP server and result schemas. Invoke with
`source="claude_mcp"` so audit records preserve origin.

Mandatory compatibility:

- outbound blocker/incident call;
- multi-turn grounded discussion;
- structured decision returned to the calling Claude turn;
- authentication handoff;
- event/status lookup.

Claude hooks can independently report:

- `PermissionRequest` for interactive permission decisions;
- `PermissionDenied` for auto-mode policy denials;
- `PostToolUseFailure` for failed tools;
- `Stop` when a continuation instruction is useful;
- `StopFailure` for authentication, rate-limit, and provider failures.

The optional hook executable is installed with the Python package:

```powershell
uv tool install --editable --force .
Get-Command agent-hotline-claude-hook
```

Merge `integrations/claude/settings.example.json` into the desired Claude settings file.
Every example handler is an async command hook. The adapter accepts bounded JSON on stdin,
submits a sanitized `source="claude_hook"` event, emits no hook-control output, and fails
open if the daemon is unavailable. It never opens the transcript path, embeds a token in
settings, returns `allow`, `deny`, `retry`, or `block`, or treats a phone response as a
Claude permission decision.

Routine `Stop` events are ignored. A stop alert requires `[HOTLINE]` in Claude's final
message or an explicit statement that human input is needed, and is suppressed while
background work remains. `StopFailure` is an alert/recovery trigger only; Claude Code
ignores its hook output and the failed turn must be resumed independently.

Use the MCP `contact_human` tool, not a hook, when a structured phone decision must return
to the current Claude turn. Hook behavior and installation details are documented under
`integrations/claude/`; event schemas come from the official
[Claude Code hooks reference](https://code.claude.com/docs/en/hooks).

Deep inbound multi-session enumeration, interruption, and root creation are not promised for
Claude. The MVP performs those operations through Codex App Server. Do not describe MCP
alone as a universal remote-control API for arbitrary Claude sessions.

## Example escalation

The agent should send facts, not a conversational script:

```json
{
  "kind": "compute_interrupted",
  "severity": "high",
  "summary": "The demo training worker ended after a checkpoint.",
  "question": "Resume the demo run, switch its mode, or stop?",
  "source": "claude_mcp",
  "dedupe_key": "demo-training-checkpoint-v1",
  "context": {
    "task_summary": "Deterministic training demonstration.",
    "agent_summary": "A recent checkpoint is available.",
    "owner_constraints": ["Do not use production credentials."]
  }
}
```

Samvaad owns the spoken turn-taking. The calling model receives only the structured result.

## Authentication handoff

Use `request_authentication` with a legitimate provider-created browser/device handoff. A
safe interaction says where to complete sign-in and waits for independent success. It never
asks the owner to dictate passwords, OTPs, MFA codes, private keys, or recovery codes.

For AWS, prefer the official browser/device flow generated by the AWS CLI. Agent Hotline
may communicate a safe handoff reference, but secrets stay in the browser/CLI session.

## Compatibility test

With the daemon running:

1. In Codex, call `hotline_status`.
2. In Claude, call `hotline_status`.
3. Run one offline `contact_human` flow from each client.
4. Verify both results have the same fields and different `source` values.
5. Only then run one native outbound call from the chosen demo client.

No client may infer approval from `failed`, `timed_out`, `no_answer`, `busy`, `deferred`, or
vague natural-language text.
