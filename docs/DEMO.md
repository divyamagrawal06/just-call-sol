# Demo runbook

The demo proves one outcome: a blocked coding agent calls its owner, answers a follow-up
from live evidence, receives a scoped decision, and resumes without the owner opening the
laptop.

## Sarvam Epoch gate-check tool

The same MCP server exposes a CSV-backed read-only demo tool:

- `check_event_registration(query)` returns a `YES`, `NO`, or `HOLD` verdict, the
  registration reason, check-in state, and gate note.
- `search_event_registrations(query, limit)` finds candidates when a name or organization
  is incomplete.

The default data source is
`outputs/sarvam_epoch_event_tracker/sarvam_epoch_registration_tracker.csv`. Override it
with `SARVAM_EPOCH_CSV` if the file is moved.

After installing the local package/plugin, restart the agent host so it refreshes the MCP
tool list. A reliable stage prompt is:

```text
There is a dispute at the Sarvam Epoch entrance. Check whether SEP-26003 is approved.
Give only the verdict, recorded reason, and gate instruction.
```

Expected verdict: `NO`. For an ambiguity demonstration, query `Rohan Mehta`; the tool
returns `HOLD` and asks for a registration ID because two records share that name.

## Fixed scenario

A deterministic Codex task has completed a rate-limiter change. Its tests pass, but the
mock deployment reports exhausted database request units. The agent needs a decision:
temporarily raise the demo limit and continue, or leave the mock deployment paused.

The only executable action shown is `demo.increase_db_ru_limit`. It affects no real
database, deployment, cloud account, or cost.

## Pre-demo go/no-go

Run from the repository root:

```powershell
uv sync --python 3.12 --extra dev
uv run pytest -q
uv run ruff check .
uv run agent-hotline doctor --live
```

Go only if:

- tests and lint pass;
- daemon, Sarvam, and public tools are healthy;
- the quick-tunnel URL matches the Agent Studio tool URLs;
- the committed version exactly matches `SARVAM_APP_VERSION`;
- the destination was verified without displaying it;
- active-call limit is one;
- real actions are disabled;
- the deterministic event is reset;
- the phone is charged, audible, and not in a blocked-call mode;
- the backup recording is locally available.

If any external gate is red, show the offline harness and the prior genuine recording. Say
which path is live and which is recorded.

## Start services

Terminal A:

```powershell
uv run agent-hotline serve
```

Terminal B:

```powershell
cloudflared tunnel --url http://127.0.0.1:8787
```

If the hostname changed, update `PUBLIC_BASE_URL` and all nine tools in the deployed Agent
Studio version, then restart Terminal A.

Terminal C:

```powershell
uv run agent-hotline doctor --live
uv run agent-hotline events --limit 10
```

## Offline rehearsal

```powershell
uv run agent-hotline demo --auto-decide
```

Expected:

- one deterministic event;
- no PSTN call;
- a scoped synthetic decision;
- the mock RU runbook verifies;
- timeline metrics render;
- rerunning does not create an uncontrolled duplicate.

This is a simulation and must be labeled as such.

## Live run

```powershell
uv run agent-hotline demo
```

Expected sequence:

1. The task changes to `BLOCKED`.
2. Hotline persists the snapshot and starts Instant Outbound.
3. The owner’s phone rings.
4. Samvaad opens with the incident summary and calls `get_context`.
5. The owner asks: “Explain exactly what changed in the retry logic first.”
6. Samvaad answers only from the stored diff/test evidence.
7. The owner says: “Raise the demo limit, rerun the full tests, then continue. Do not run
   migrations. Call again if the demo still reports server errors.”
8. Samvaad reads back the instruction and constraints.
9. The owner gives a clear confirmation and enters the configured owner PIN by DTMF for
   the decision.
10. For the mock registered action, Samvaad calls `prepare_action`, reads the exact phrase,
    and calls `confirm_action` with the ephemeral DTMF PIN.
11. Only the returned one-time grant is passed to `execute_action`; the mock runbook reports
    verified completion.
12. Samvaad calls `record_decision` separately with the resulting instruction, an ephemeral
    DTMF PIN, and no action ID. This wakes the original request but does not grant the
    registered action.
13. The agent resumes within the confirmed instruction.
14. The callback later reconciles status/transcript.

The visible timeline should end:

```text
BLOCKED -> CALLING -> DISCUSSING -> CONFIRMED -> RESUMED -> COMPLETED
```

Show the measured time to decision and time to resume. Do not claim a general performance
improvement from one sample.

## Three-minute stage script

### 0:00–0:30 — problem

“Autonomous coding agents can work for hours, then sit blocked because their owner stepped
away. Agent Hotline turns the phone number into a narrow control plane: the agent calls,
explains live evidence, gets a scoped decision, and continues.”

### 0:30–0:45 — safety

“Voice is not authority. No answer is no approval, and operational actions are typed,
expiring, and read back before confirmation.”

### 0:45–2:35 — live interaction

Run the fixed scenario. Let the follow-up question demonstrate live context retrieval.
Keep the call natural; do not narrate architecture over the conversation.

### 2:35–2:55 — result

Show the task resuming and the timeline metric.

### 2:55–3:00 — secondary proof

Show either Claude discovering the same MCP tools or one inbound Codex task inspection.
Do not attempt both live.

## Failure recovery

| Failure | Stage response |
| --- | --- |
| Phone does not ring | Stop after the bounded attempt; show recorded ring/tool flow |
| Tool lookup fails | Samvaad must say context is unavailable; show stored event locally |
| Call drops before decision | Show safe non-approved state; use recording |
| Callback is late | Continue from mid-call decision; explain reconciliation is asynchronous |
| Provider/model error | Show watchdog-created event and safe paused state |
| Tunnel changed | Do not edit live under time pressure; use recording/offline harness |
| Ambiguous speech | Clarify; never force an approval |

Do not repeatedly redial on stage.

## Backup recording

Create the backup only from a real validated call. Capture:

- the phone ringing without revealing its number;
- at least three conversational turns;
- a context-tool lookup;
- exact readback/confirmation;
- the originating agent resuming;
- the final timeline and metric.

Crop/redact caller IDs, provider identifiers, private paths, tokens, and terminal
environment output. State clearly that the video is a pre-recorded fallback.

## Post-demo evidence

```powershell
uv run agent-hotline events --limit 10 --json
```

Record only opaque event/attempt references and aggregate timings in submission material.
Do not publish the raw transcript, phone numbers, credentials, or callback URL.
