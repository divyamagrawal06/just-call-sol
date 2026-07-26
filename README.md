# Agent Hotline

Agent Hotline is a local control plane that lets Codex or Claude call their owner when
work is blocked, and lets the owner call back to inspect, steer, interrupt, or create
agent work.

The runtime is deliberately split into:

- a persistent FastAPI + SQLite daemon;
- a shared stdio MCP server for Codex and Claude;
- a CLI for setup, diagnostics, demos, and operations;
- a supervised Codex App Server adapter for deep thread control;
- a managed Sarvam Samvaad agent for PSTN, speech, turn-taking, interruption, and TTS.

The central safety rule is simple: voice is an interface, not an authority boundary.
No answer is never approval, and destructive actions require a registered action,
exact readback, a scoped expiring grant, and a daemon-verified owner PIN.

For a blocking call that ends without a decision, an optional missed-call fallback can send
a generic notification to an owner-controlled SMS/push webhook. Its one-time link keeps the
bearer in the URL fragment, reveals context only after PIN verification, expires durably,
and can record a decision but never authorize a registered action. See
`docs/FALLBACK.md`.

## Development

```powershell
uv sync --python 3.12 --extra dev
uv run pytest
uv run ruff check .
uv run agent-hotline doctor
uv run agent-hotline-sarvam tools-manifest --format markdown
uv run agent-hotline-sarvam ensure-deployment
```

`ensure-deployment` is read-only unless `--apply` is supplied. It always uses
`SARVAM_APP_VERSION`, refuses to silently replace an older/different binding, and reports
the version it verified. When `SARVAM_INBOUND_SCHEDULE` is omitted, the desired deployment
uses Sarvam's 24/7 default; when set to one JSON schedule object, that schedule is included
in the same exact reconciliation check. The generated Samvaad manifest contains `begin_inbound`,
`get_context`, `record_decision`, `prepare_action`, `confirm_action`, `execute_action`,
`list_threads`, `inspect_thread`, and `repo_context`. Adding `repo_context` to an already
deployed app requires a new committed Agent Studio version; generating the manifest does not
mutate the live app.

`query_repository_context` gives Codex and Claude the same bounded read-only evidence path
used by `repo_context`: Git status, diff summaries, literal search, bounded file reads, and a
static test inventory. It is confined to explicit Git roots in `HOTLINE_WORKSPACE_ROOTS` (or
an explicitly configured `CODEX_APP_SERVER_CWD` Git root), redacts and caps all output,
rejects traversal and credential files, never accepts a command, and never executes tests.
The public voice route additionally requires the ephemeral daemon-verified owner PIN.
Exposing repository evidence durably makes that call event evidence-only: a fresh call is
required before any decision or registered action can be authorized.

`record_decision` uses dynamic outcome, instruction, and ephemeral PIN fields. Its live
schema keeps constraints/action IDs empty. Every outcome requires the owner PIN and a
provider-correlated live session. Registered actions are authorized by `confirm_action` and
the one-time grant consumed by `execute_action`.

Configuration is environment-based. Copy `.env.example` to an ignored `.env` for
development, or use your operating system's secret store/environment. Never commit
phone numbers, API keys, confirmation secrets, transcripts, or provider identifiers.

Detailed setup, architecture, threat boundaries, Claude/Codex installation, the live demo
procedure, and a sanitized verification record are under `docs/`.
