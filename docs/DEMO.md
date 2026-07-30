# Demo and acceptance walkthrough

Keep mocked validation and live acceptance visibly separate. The offline demo proves local
state and action contracts; only a real PSTN call can validate the production voice path.

## Offline smoke test

```powershell
uv run agent-hotline demo --auto-decide
```

Expected output includes:

```json
{
  "mode": "deterministic_mock",
  "real_resources_changed": false,
  "verified": true
}
```

This mode:

- forces the fake transport;
- creates no Twilio or OpenAI call;
- uses a generated mock-only PIN;
- keeps Codex writes and real runbooks disabled;
- executes only the deterministic mock database-RU runbook.

Do not label it live or end-to-end.

## Two-minute live demo

Prepare a known Codex task in an allowlisted workspace and keep
`HOTLINE_ALLOW_CODEX_WRITES=false`.

### 0:00–0:20 — show readiness

```powershell
uv run agent-hotline doctor --live
```

Say: “This proves the local daemon and provider APIs are reachable. The next phone call is the
actual voice-path test.”

### 0:20–1:05 — inbound conversation

Call the configured Twilio number from the owner phone.

Ask:

> What Codex tasks are active? Inspect the task about the Realtime migration and tell me its
> current state.

Interrupt the agent once while it is answering, then ask a follow-up. The useful proof is that
Realtime holds a natural conversation and uses bounded task tools rather than reciting a
script.

If you ask it to modify the task while the Codex-write gate is off, it should explain that the
write is unavailable. That is the expected production-safe default.

### 1:05–1:45 — outbound decision

From another terminal:

```powershell
uv run agent-hotline call `
  --kind compute_interrupted `
  --severity high `
  --summary "A preemptible compute instance ended the training run." `
  --question "Retry once with the same configuration, or leave it paused?"
```

On the call, discuss one constraint, then choose an instruction. The agent should read the
server-generated decision back exactly, wait for your later reply, ask for keypad PIN entry,
and return a structured result to the waiting command.

### 1:45–2:00 — show the receipt

```powershell
uv run agent-hotline events --limit 5
uv run agent-hotline result evt_... --watch
```

Show the event summary and exact structured result. Explain that silence, voicemail, failure,
or an unfinished verification would have returned no approval.

## Optional write demo

Only after the read-only live path passes, set:

```dotenv
HOTLINE_ALLOW_CODEX_WRITES=true
```

Restart the daemon, call inbound, and request one exact Codex task instruction or interruption.
The voice agent must:

1. resolve one exact task and turn where applicable;
2. prepare the action;
3. speak the exact server readback;
4. hear a later explicit owner response;
5. arm and complete a fresh keypad PIN;
6. confirm the one-time grant;
7. execute once and report the actual tool result;
8. record the final instruction and action result for the waiting agent.

Do not demo a file-change callback; it always declines by design. Do not claim to control AWS,
databases, deployments, or batch systems: the repository ships no real infrastructure
runbooks.

## Abort conditions

Stop the demo and keep work paused if:

- provider signatures fail;
- the webhook and SIP call do not correlate;
- the wrong task is resolved;
- the readback is incomplete or changes after confirmation;
- PIN digits appear in model-visible text or logs;
- the write gate is unexpectedly enabled;
- the tool reports an error or times out.

Never fill in a missing result with narration. A visible fail-closed outcome is more accurate
than claiming success.
