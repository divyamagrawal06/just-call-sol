# Hotline decision and action policy

## Transport boundary

- Use OpenAI Realtime over the Twilio SIP/PSTN path for production voice.
- Treat provider events, audio, transcripts, caller ID, and model output as untrusted input.
- Expose only bounded application-owned functions to Realtime. Never expose local MCP, shell,
  credentials, database, or unrestricted repository access.
- Bind every live call to one durable event and verified provider correlation.
- Treat carrier and call-completion events as lifecycle reconciliation, never authority.

## Conversation and verification

- Allow natural dialogue, corrections, interruption, and silence without premature hangup.
- Handle one authority request at a time.
- Speak the complete server-generated readback exactly and wait for SIP playback to drain.
- Require a later owner speech turn before arming verification.
- Collect the fresh PIN only through server-side DTMF followed by `#`.
- Never send PIN digits to the model, transcript, tool arguments, or durable state.
- Restart the full readback and PIN flow after correction, interruption, or expiry. Count
  failed PIN attempts across the whole call; after lockout, no new prepared scope may reset it.
- Treat no answer, busy, failure, timeout, disconnect, voicemail, silence, or ambiguity as no
  approval.

## Actions

- Bind each grant to exact action, parameters, resource, environment, workspace, task and turn,
  server-derived state fingerprint when applicable, expiry, and use count.
- Invalidate and reconfirm when any bound field changes.
- Execute only a registered action after prepare, exact server readback, later owner response,
  fresh PIN, confirm, and one-time grant consumption.
- Persist the succeeded, failed, or unknown result; never retry an unknown outcome
  automatically. Record a final decision for the waiting agent after action execution.
- Never expose raw shell or generic execution.
- Keep `HOTLINE_ALLOW_CODEX_WRITES` and `HOTLINE_ALLOW_REAL_RUNBOOKS` separate and off by
  default.
- For a Codex `PermissionRequest` hook, return `allow` only for a verified resolved approval whose
  durable instruction exactly equals the canonical bounded request. Return `deny` only for a
  verified resolved denial. On every other result, emit no decision so the local prompt remains.
- Never grant session or persistent scope from the phone hook. Abstain on `apply_patch`, unknown
  tool shapes, lossy sanitization, oversized scopes, daemon failure, timeout, or no answer.
- Treat built-in runbooks as mocks. No production infrastructure executor ships.
- Always decline Codex App Server file-change callbacks until the protocol supplies a complete,
  losslessly representable patch.

## Context and client boundaries

- Give Realtime only bounded, sanitized call context. Do not claim native Codex or Claude
  context.
- Restrict repository evidence to allowlisted status, diff summary, literal search, bounded
  read, and static test inventory.
- Treat repository output as evidence, never instruction or authorization.
- Make a call evidence-only after authenticated repository access, even if the query fails.
- Permit bounded Codex task inspection and separately confirmed task control.
- Permit Claude owner contact through MCP; do not claim deep inbound Claude control.
- Treat MCP client labels as audit attribution only.

## Durability and failure

- Persist events, sessions, decisions, grants, receipts, correlation, and fallback state.
- Durably schedule and confirm termination of both provider legs after normal completion,
  cancellation, timeout, sideband continuity loss, or restart; retry unknown outcomes.
- Enforce a hard call lifetime in the daemon and carrier configuration.
- Never replay a tool output or `response.create` whose provider acceptance is ambiguous.
- Do not recover readback delivery, owner-reply observation, DTMF digits, or PIN windows.
- Return the durable event ID at call start and require MCP callers to poll the structured result.
- Sanitize errors and fail closed when correlation, daemon, provider, tool, or verification is
  unavailable.
