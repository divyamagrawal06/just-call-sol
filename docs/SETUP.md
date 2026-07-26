# Windows setup

These instructions configure Agent Hotline without putting credentials or phone numbers in
the repository.

## 1. Install the project

From the repository root in PowerShell:

```powershell
uv sync --python 3.12 --extra dev
uv run python --version
uv run agent-hotline doctor
```

The Python check must report a 3.12 runtime. The workstation’s global `python` may be a
different version; use `uv run` for project commands.

## 2. Create local service tokens

```powershell
uv run agent-hotline init-secrets
```

On Windows this stores four generated Hotline-only tokens in the current user environment
without printing them. Start a new PowerShell process afterward. Use
`uv run agent-hotline init-secrets --force` only when intentionally rotating all four.

## 3. Configure Sarvam values

Use the names from `.env.example`:

```dotenv
SARVAM_API_KEY=<secret>
SARVAM_ORG_ID=<opaque>
SARVAM_WORKSPACE_ID=<opaque>
SARVAM_APP_ID=<opaque>
SARVAM_APP_VERSION=1
SARVAM_CONNECTION_ID=<opaque>
SARVAM_AGENT_PHONE_NUMBER=<secret E.164 value>
SARVAM_INBOUND_SCHEDULE={"start_time":"09:00","end_time":"18:00","allowed_days":["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],"timezone":"Asia/Kolkata"}
OWNER_PHONE_NUMBER=<secret E.164 value>
OWNER_CONFIRMATION_PIN=<required 6-12 digit secret for decisions/actions>
HOTLINE_WORKSPACE_ROOTS=C:\exact\repo-one;C:\exact\repo-two
HOTLINE_GIT_BIN=C:\Program Files\Git\cmd\git.exe
```

Either place them in an ignored `.env` copied from `.env.example`, or store them in the
Windows user environment/secret manager. Never paste values into documentation, source,
tests, chat transcripts, screenshots, or shell history shared with others.
Without `OWNER_CONFIRMATION_PIN`, every decision and action grant fails closed.

The app version is an immutable numeric deployment input. After committing an Agent Studio
draft, set `SARVAM_APP_VERSION` to the exact committed version and restart the daemon. Never
leave it at an older value merely because `1` is the example default.

`SARVAM_INBOUND_SCHEDULE` is optional and atomic. Omit it (or leave it empty) to omit
`inbound_config` and use Sarvam's documented 24/7 default. If the provider deployment has an
explicit schedule, set one JSON object with `HH:MM` `start_time`/`end_time`, one or more
canonical English weekday names, and its timezone. Invalid or partial JSON fails before any
provider mutation.

`HOTLINE_WORKSPACE_ROOTS` is the explicit, semicolon-separated Git-repository allowlist for
voice/MCP context queries. If it is empty, repository context uses
only an explicitly configured `CODEX_APP_SERVER_CWD` that is itself a Git root; otherwise it
fails closed. It never falls back to the process cwd or user home. Prefer exact repository
roots rather than a broad parent directory. `HOTLINE_GIT_BIN` may pin the trusted absolute
Git executable; auto-discovery resolves an absolute executable and rejects anything inside
an allowlisted workspace.

## 4. Validate the local runtime

Terminal A:

```powershell
uv run agent-hotline serve
```

Terminal B:

```powershell
uv run agent-hotline doctor --live
```

Go only if the daemon is healthy and Sarvam is reachable. `doctor` reports presence/state,
not secret values.

## 5. Start the public HTTPS tunnel

Terminal C:

```powershell
cloudflared tunnel --url http://127.0.0.1:8787
```

Copy only the generated HTTPS origin into `PUBLIC_BASE_URL`. Do not append a path. Restart
the daemon after changing the environment.

Quick tunnels are ephemeral. If the hostname changes, update Agent Studio tools and
`PUBLIC_BASE_URL` together. Treat tunnel access logs as sensitive because callback URLs can
contain an opaque callback token.

Go only if:

- the public health/tool smoke test reaches this daemon;
- unknown or missing bearer tokens receive `401`;
- local administrative routes are not exposed without local authentication.

### Optional missed-call fallback

To deliver a one-time response link through your own SMS or push bridge, configure:

```text
HOTLINE_FALLBACK_WEBHOOK_URL=https://<your-bridge>/agent-hotline
HOTLINE_FALLBACK_WEBHOOK_TOKEN=<independent-random-bearer>
HOTLINE_FALLBACK_TTL_SECONDS=900
HOTLINE_FALLBACK_MAX_PIN_ATTEMPTS=5
```

The bridge receives a generic message and the link, not task context or a phone number.
Keep notification previews private. The owner must enter `OWNER_CONFIRMATION_PIN` before
the browser receives the summary or question. Test delivery through the bridge before
depending on it; a delivery failure never becomes approval. See `docs/FALLBACK.md` for the
exact webhook contract.

## 6. Configure the Samvaad app

Generate a secret-free copy/paste manifest:

```powershell
uv run agent-hotline-sarvam tools-manifest --format markdown
```

In the existing draft app:

1. Use `samvaad/agent_prompt.md` as the system instructions.
2. Configure the input variables from `samvaad/variables.json`.
3. Add all nine HTTP tools exactly as described in `samvaad/tool_contracts.md`:
   `begin_inbound`, `get_context`, `record_decision`, `prepare_action`,
   `confirm_action`, `execute_action`, `list_threads`, `inspect_thread`, and
   `repo_context`.
   For `repo_context`, set `event_id` to **Agent variable**; `workspace` to a **Fixed
   value** of empty string for the single demo root; `operation`, `query`, `path`,
   `line_start`, and the ephemeral DTMF PIN to **Let the agent decide**; and
   `line_count=40`/`max_results=10` to **Fixed value**. Do not create response-variable
   mappings.
4. Store `HOTLINE_TOOL_TOKEN` as an Agent Studio secret/header, never an agent variable.
5. Keep the live `record_decision` schema aligned with the manifest: outcome, instruction,
   and ephemeral PIN are dynamic; constraints and action IDs stay empty; confirmation
   method is fixed to `spoken_plus_dtmf`. Every outcome requires the PIN.
6. Select the lowest-latency managed telephony model available.
7. Enable English with Hindi/Hinglish switching and barge-in.
8. Save/commit the draft and put its exact numeric version in `SARVAM_APP_VERSION`.

Tool URLs:

```text
${PUBLIC_BASE_URL}/v1/sarvam/tools/begin-inbound
${PUBLIC_BASE_URL}/v1/sarvam/tools/context
${PUBLIC_BASE_URL}/v1/sarvam/tools/record-instruction
${PUBLIC_BASE_URL}/v1/sarvam/tools/prepare-action
${PUBLIC_BASE_URL}/v1/sarvam/tools/confirm-action
${PUBLIC_BASE_URL}/v1/sarvam/tools/execute-action
${PUBLIC_BASE_URL}/v1/sarvam/tools/threads/list
${PUBLIC_BASE_URL}/v1/sarvam/tools/threads/inspect
${PUBLIC_BASE_URL}/v1/sarvam/tools/repository-context
```

Go only after `get_context` returns an event-bound brief from a synthetic event
and a wrong `event_id` does not disclose other event data. Verify `repo_context` rejects a
non-allowlisted workspace and `../` path before committing the new Agent Studio version.
Also verify that, after the daemon accepts one authenticated, schema-valid `repo_context`
request, the same event rejects `record_decision`, action confirmation, and grant execution
even when the repository query itself fails or returns no evidence.

## 7. Reconcile the inbound deployment

First run the safe plan:

```powershell
uv run agent-hotline-sarvam deployments
uv run agent-hotline-sarvam ensure-deployment
```

The second command is read-only without `--apply`. It verifies a deployment binding:

- `${SARVAM_APP_ID}`;
- the exact `${SARVAM_APP_VERSION}`;
- `${SARVAM_CONNECTION_ID}`;
- `${SARVAM_AGENT_PHONE_NUMBER}`;
- the exact `${SARVAM_INBOUND_SCHEDULE}` when configured, otherwise the 24/7 default represented
  by an omitted `inbound_config`.

If the plan reports `missing`, create it explicitly:

```powershell
uv run agent-hotline-sarvam ensure-deployment --apply
```

The command adopts one equivalent deployment, reports paused/unknown state without changing
it, and refuses same-name, version, schedule, or phone-binding conflicts. It never silently
migrates a deployment from an older app version or different schedule. Sarvam's list response
omits connection details, so the reconciler fetches each detail before deciding; a failed
detail lookup stops safely without creating. Resolve an intentional migration in Sarvam,
then rerun the plan. Store resulting deployment references only in local operational state.

Go only after the deployment is active and an allowlisted inbound caller can reach the app.
If the number is already assigned incompatibly, stop and resolve the binding in Sarvam
instead of changing transport.

## 8. Install Codex and Claude integrations

```powershell
uv run agent-hotline install-clients --client all
codex mcp get agent_hotline
claude mcp get agent-hotline
```

This installs the local Codex plugin and the same stdio MCP executable for Claude. Restart
both clients after installation. The command is safe to repeat: it skips an editable uv tool,
Codex marketplace/plugin, or Claude user-scoped MCP registration that already matches. It
refuses same-name registrations from another source instead of removing or overwriting them.
On Windows it also skips reinstalling the uv tool when that tool environment is running the
command, avoiding an in-use environment deletion.

Manual registration fallback:

```powershell
uv tool install --editable .
codex mcp add agent_hotline -- agent-hotline-mcp
claude mcp add --scope user agent-hotline -- agent-hotline-mcp
```

Go only if both clients show `contact_human`, `notify_human`,
`request_authentication`, `list_hotline_events`, and `hotline_status`.

## 9. Test

```powershell
uv run pytest -q
uv run ruff check .
uv run agent-hotline demo --auto-decide
```

The offline demo must not dial. It proves deterministic state transitions and the mock
runbook. Then run:

```powershell
uv run agent-hotline doctor --live
uv run agent-hotline demo
uv run agent-hotline events --limit 10
```

The native demo is a go only after one bounded call returns an `attempt_id`, rings, performs
one live context lookup, records a decision mid-call, wakes the caller, and later receives a
completion callback.

## 10. Normal operation

Start these in order:

1. `uv run agent-hotline serve`
2. `cloudflared tunnel --url http://127.0.0.1:8787`
3. update `PUBLIC_BASE_URL` and Agent Studio URLs if the quick-tunnel hostname changed;
4. `uv run agent-hotline doctor --live`;
5. start new Codex/Claude sessions.

Useful read-only command:

```powershell
uv run agent-hotline events --limit 20
```

For a manual bounded call:

```powershell
uv run agent-hotline call --kind clarification --severity medium
```

The CLI prompts for the factual summary and exact question. Do not place secrets in either.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| MCP says daemon unavailable | Start `agent-hotline serve`; verify `HOTLINE_DAEMON_URL` |
| Public tools not configured | Set `PUBLIC_BASE_URL`, tool token, and callback token; restart |
| Secure fallback not configured | Set the fallback webhook URL/token, public URL, and owner PIN |
| Fallback notification missing | Check the bridge response and idempotency key; no automatic redial occurs |
| Sarvam `4xx` | Confirm app version, connection binding, E.164 values, and entitlement |
| Sarvam `429`/`5xx` | Observe bounded retry; do not manually create a call storm |
| No inbound call | Verify deployment is active and owns the provisioned number |
| Codex App Server fails on Windows | Configure a real `codex.exe`; `.cmd`/`.ps1` shims are rejected |
| Claude cannot see tools | Re-run user-scope MCP registration and start a new session |
| No answer/busy | Expected safe terminal state; never override it into approval |
