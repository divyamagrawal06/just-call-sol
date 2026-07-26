# Claude Code hook bridge

The shared MCP server is the primary Claude integration. This optional command hook
bridge independently alerts the owner when Claude is waiting for permission or stops
after a significant failure.

Install the project so the hook executable is on `PATH`:

```powershell
uv tool install --editable --force .
```

Merge `settings.example.json` into either:

- `.claude/settings.json` for a shareable project configuration; or
- `~/.claude/settings.json` for your own Claude Code sessions.

Keep the hooks as asynchronous command hooks. Claude Code ignores decision output from
async hooks, and `agent-hotline-claude-hook` itself writes no stdout or stderr and exits
successfully even when the daemon is unavailable. The normal Claude permission dialog and
permission policy therefore remain authoritative.

## Event mapping

| Claude event | Hotline behavior |
| --- | --- |
| `PermissionRequest` | Places a non-authoritative approval alert; approve or deny in Claude Code |
| `PermissionDenied` | Notifies; never retries or reverses the denial |
| `PostToolUseFailure` | Calls for authentication/provider failures; otherwise notifies |
| `StopFailure` | Calls for API authentication, rate-limit, billing, model, or provider failure |
| `Stop` | Calls only when the final message contains `[HOTLINE]` or clearly requests human input |

`Stop` is ignored while Claude reports background tasks or scheduled work, and repeated
stop-hook continuations are ignored. Routine successful stops do not consume a call.

The adapter accepts only documented fields, caps stdin at 1 MiB, never opens
`transcript_path`, hashes the Claude session ID, and redacts credential-like values and
phone numbers. Neither hook configuration nor its command arguments contain a Hotline
token; the local daemon client reads `HOTLINE_LOCAL_TOKEN` from the environment.

For a phone decision that must return to the current Claude turn, use the MCP
`contact_human` tool. Hook calls are side-channel alerts only and never constitute an
approval. Passwords, OTPs, MFA codes, recovery codes, and private keys must stay in the
legitimate browser or device flow.

See the official [Claude Code hooks reference](https://code.claude.com/docs/en/hooks) for
event schemas and the current settings-file locations.
