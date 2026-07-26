# Security model

Agent Hotline lets speech influence coding work, so its security boundary is stricter than
an ordinary voice bot. Voice is an interface, not an authority boundary.

## Non-negotiable invariants

- Caller ID and voice recognition alone do not prove identity.
- No answer, voicemail, silence, busy, disconnect, timeout, malformed tool output, or
  provider failure is never approval.
- Free-form speech never becomes a shell command.
- Only registered, typed runbooks can perform operational actions.
- High-risk actions require exact readback, a scoped expiring confirmation, and a second
  factor.
- A grant is valid for one exact action and normally one use.
- Target or state drift invalidates the grant.
- Authentication calls never request passwords, OTPs, MFA codes, private keys, recovery
  codes, or session cookies.
- Real actions remain disabled unless an operator explicitly enables the runtime gate.

## Trust boundaries

| Boundary | Authentication | Allowed data |
| --- | --- | --- |
| MCP/CLI to local daemon | `HOTLINE_LOCAL_TOKEN` over loopback | Sanitized event/repository context and structured decisions |
| Samvaad HTTP tools to daemon | `HOTLINE_TOOL_TOKEN` over HTTPS | Event-scoped briefs, bounded repository evidence, and action contracts |
| Sarvam callback to daemon | Callback-token binding plus idempotency | Normalized attempt status/transcript |
| Daemon to Sarvam | `SARVAM_API_KEY` | Minimal call configuration and event variables |
| Daemon to Codex App Server | Direct child stdio pipes | Safe allowlisted task/turn methods |
| Owner to decision | Daemon-side constant-time PIN match on a correlated live session | One event-bound decision |
| Owner to high-risk action | Daemon-side constant-time PIN match plus exact phrase | One action-bound confirmation |
| Missed-call bridge | Independent bearer over HTTPS | Generic notice plus a short-lived fragment URL |
| Owner fallback browser | Signed fragment capability plus daemon PIN verification | One event-bound decision; no action grant |

Use independent random values for the four Hotline tokens. Never reuse the Sarvam API key
as a local, tool, callback, or fallback-webhook token.

## Secrets and personal data

The following are secrets or sensitive personal data:

- every phone number;
- Sarvam API key and all provider identifiers;
- Hotline local/tool/callback tokens;
- owner confirmation PIN;
- transcripts, voice recordings, caller metadata, private URLs, workspace paths, diffs, and
  logs that may reveal source or infrastructure.

Store values in the local environment or an operating-system secret store. `.env` and
`.hotline/` are local-only. `.env.example` contains names and safe defaults, never values.
Presence-only diagnostics are permitted.

`identity_verified` is output-only. Samvaad cannot assert it. Decision requests carry an
ephemeral, secret-typed `confirmation_pin`; the daemon compares it to
`OWNER_CONFIRMATION_PIN`, persists only the resulting boolean, and excludes the PIN from
model dumps, responses, and durable state. Caller allowlisting permits a bounded inbound
session but never sets identity.

The missed-call link is a separate non-voice channel. Its bearer remains in the URL fragment
and is removed from browser history before verification. The initial page is generic; task
context is returned only after a capped, constant-time PIN check. The daemon then issues a
shorter-lived submission capability and atomically consumes the durable fallback record
with the decision. The notification webhook receives no task context, phone number,
workspace, transcript, or PIN. Fallback decisions always keep `approved_action_ids` empty
and cannot confirm or execute registered actions.

The live `record_decision` tool varies only outcome, instruction, and the ephemeral PIN.
Its constraints/action-ID arrays remain empty and its confirmation method is fixed to
`spoken_plus_dtmf`. Every outcome requires a matching PIN and a direction-matched,
provider-correlated live session; deny, defer, instruct, and auth-completed are
authority-bearing because they wake the waiting agent.
Registered actions never receive authority from that decision record: only
`confirm_action` may issue their scoped one-time grant, and `execute_action` consumes and
audits it.

Redact before persistence and again before speech. Avoid reading opaque identifiers aloud.
Retain full transcripts/recordings only when needed for the demo and with the caller’s
knowledge; delete them according to the chosen retention policy.

The callback token currently appears as an opaque URL path segment. Treat the full callback
URL as a secret, prevent it from entering screenshots/logs, and rotate it if exposed.
The packaged `agent-hotline serve` command disables Uvicorn access logs so this path is not
printed.

## Repository evidence boundary

Repository context is an operation API, not a command API. Its only operations are Git
`status`, Git `diff` summary, literal `search`, bounded `read`, and static `tests` inventory.
It never runs tests and never accepts a shell command. Git is invoked with a fixed argv,
`shell=False`, external diff/text conversion disabled, prompts disabled, and a short timeout.

Workspace roots are explicit Git-root allowlist entries; there is no process-cwd/home
fallback. Every public request requires the daemon-verified ephemeral owner PIN. Public
outbound requests are additionally forced to the workspace stored on their live event and
fail closed if it is absent; their live session carries the persisted provider `attempt_id`.
Inbound requests require an allowlisted inbound event and a session carrying the persisted
provider `interaction_id` in `CONNECTED` or `DISCUSSING`. The daemon validates this
direction/state/correlation boundary before comparing the PIN, and the public repository
route is separately capped at six attempts per minute per client.
Relative paths are resolved under the chosen root. Traversal, absolute paths, symlink,
junction/reparse-point escapes, secret/credential files, binary or oversized files, and
common generated/vendor directories are rejected. Reads are capped at 80 lines, searches at
20 matches plus file/byte/entry scan budgets, Git output at 128 KiB, and the entire response
at 6,000 serialized characters. Git resolves to an absolute non-workspace executable and
receives a minimal environment. Known runtime secrets, secret-shaped environment
assignments, credentialed URLs, phone numbers, metadata, and instruction-like
prompt-injection strings are redacted or neutralized. The result explicitly remains
untrusted evidence and confers no authority.

Git metadata must be a real `.git` directory directly below the allowlisted root. Gitfiles,
linked worktrees, submodule-style roots, symlinked metadata, junctions, mount points, and
reparse-point metadata are intentionally unsupported.

The public repository route appends a durable `repository_context_exposed` audit entry
before attempting the bounded filesystem/Git query. Storage transactions reject every later decision, action
preparation, action confirmation, and grant consumption for that event. This same-event
separation is the enforceable prompt-injection boundary; phrase filtering and model
instructions remain defense in depth. A fresh call without repository evidence is required
for authority.

## Approval protocol

For a benign instruction:

1. Retrieve current context.
2. Restate the exact instruction and constraints.
3. Receive a clear confirmation and the owner PIN by DTMF.
4. Persist through `record_decision` with the ephemeral PIN.
5. Continue only after `accepted: true`.

For a medium/high-risk action:

1. Resolve an exact registered `runbook_id`.
2. Strictly validate typed parameters; reject unknown fields and coercion.
3. Bind the preview to event, workspace, task, commit/state, environment, resource, runbook
   revision, normalized parameters, and expiry.
4. Return `exact_readback`, action hash, and an expiring nonce.
5. Verify the allowlisted owner and configured second factor.
6. Match the exact confirmation phrase.
7. Issue a one-time grant.
8. Recompute the action hash immediately before execution.
9. Atomically consume the grant.
10. Verify and audit the outcome.

Do not interpret “yes,” “do it,” or “take it down” as sufficient confirmation for an
ambiguous or destructive target.

## Runbook policy

The committed demo registry contains only:

- `demo.increase_db_ru_limit`
- `demo.pause_deployment`
- `demo.terminate_batch_runs`

Each is bounded to named demo resources and reports mock execution. Inputs such as
`environment=production`, an arbitrary resource, an unknown field, or a command string are
rejected.

Adding a real runbook requires a separate security review, typed parameters, explicit
allowlists, deterministic preview, verification, rollback guidance, least-privilege
credentials, and an intentional `HOTLINE_ALLOW_REAL_ACTIONS=true` deployment decision.
Changing that flag does not authorize an unregistered action.

## Prompt-injection defense

Code, issue text, logs, diffs, test output, transcripts, and tool responses are untrusted.
The voice prompt instructs Samvaad to treat them as evidence only. Tool results should be
structured and concise. Never place policy text, bearer tokens, or hidden instructions in
agent variables.

The daemon enforces policy independently of the model. Even a fully compromised prompt
cannot:

- add a new runbook;
- change typed bounds;
- forge an action hash;
- bypass expiry/replay checks;
- access App Server shell methods;
- turn a failed call into approval.

## Network and API controls

- Bind the daemon to `127.0.0.1` by default.
- Expose only required tool and callback routes through the tunnel.
- Require HTTPS for public tool/callback configuration.
- Reject missing/incorrect bearer tokens with no event disclosure.
- Enforce request size limits and strict Pydantic schemas.
- Use constant-time comparisons for secrets/hashes.
- Rate-limit by caller, event, destination, and active-session count.
- Deduplicate before contacting Sarvam; Instant Outbound has no documented idempotency
  header.
- Use bounded exponential backoff only for transient network/`429`/`5xx` outcomes.
- Never auto-redial repeatedly; honor quiet hours and explicit retry policy.
- Do not follow user-supplied callback or authentication URLs from the daemon.

## App Server controls

The adapter launches `codex app-server --stdio` without a shell. On Windows it rejects
`.cmd` and `.ps1` shims and requires a real executable.

Allowed client methods are limited to task and turn operations. Shell, command-execution,
filesystem, process-spawn, and dynamic-tool methods are absent. Task references must resolve
unambiguously, mutations bind to exact task/turn IDs, and root tasks can start only within
configured workspace roots.

Interrupting a task does not imply permission to terminate its external processes.

## Failure and recovery policy

| Condition | Safe outcome |
| --- | --- |
| No answer/busy/failed call | Pause or defer; no grant |
| Decision timeout | Return `timed_out`; preserve event |
| Tool timeout | Say context/recording failed; no claimed success |
| Duplicate event | Reuse active event; do not redial |
| Duplicate callback | Return existing receipt; no duplicate transition |
| Daemon restart | Rebuild waiters from durable state; preserve expiry |
| Provider outage | Watchdog event remains queued; do not invent contact |
| LLM provider outage | Call from persisted watchdog snapshot |
| Target changed | Reject old grant and reconfirm |
| Token exposure | Rotate affected token, invalidate sessions, audit access |

## Operational checklist

Before a live call:

- `uv run agent-hotline doctor --live` is green;
- real actions are off;
- the owner destination is correct without printing it;
- active-call limit is one;
- the demo event has a stable dedupe key;
- the snapshot is sanitized;
- the exact no-answer policy is set;
- the backup path is ready.

After a live call:

- confirm attempt/session/decision correlation;
- confirm no secret appeared in logs or transcript;
- verify the returned constraints were applied;
- verify the post-call webhook did not change authorization;
- rotate any token shown during screen sharing.
