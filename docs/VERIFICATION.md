# Verification status

This document distinguishes repository validation from a real production call. Passing local
tests does not prove provider console configuration, PSTN routing, webhook signatures, audio,
barge-in, DTMF delivery, or live Codex control.

## Automated and mocked coverage

The repository includes automated tests for settings, provider clients, Twilio HTTP and WSS
signatures, SIP and Media Streams TwiML, OpenAI webhook admission, direct PCMU bridging,
Realtime tool dispatch, state transitions, fallback, MCP stdio, Codex integration boundaries,
packaging, and plugin metadata.

Run:

```powershell
uv sync --python 3.12 --extra dev
uv run pytest -q
uv run ruff check .
uv build --wheel
uv run agent-hotline demo --auto-decide
```

The deterministic demo is explicitly mocked. A successful result proves only that the local
fake-call decision and mock-runbook flow completed.

## Live status

Live validation must be recorded against the operator's own OpenAI project, Twilio account,
phone numbers, public HTTPS origin, and Codex installation. Do not mark an item complete from
a fixture, mocked client, provider API probe, or console screenshot alone.

Before a release is described as live-validated, record dated evidence for:

- [ ] inbound Twilio signature, account, destination, direction, and caller-allowlist checks;
- [ ] signed SIP correlation and verified OpenAI incoming-call webhook, if using `sip` mode;
- [ ] authenticated WSS handshake, expiring inbound admission, outbound event correlation, and
      two-way PCMU audio, if using `media_stream` mode;
- [ ] outbound parent call creation, signed TwiML fetch with the allocated parent CallSid,
      ambiguous-response recovery, and its Twilio status callback;
- [ ] inbound `<Dial action>` delivery of `DialCallStatus` to the signed status route, if using
      `sip` mode;
- [ ] server-side Realtime WebSocket connection and natural two-way audio;
- [ ] interruption/barge-in and long-pause behavior without premature hangup;
- [ ] exact server transcript plus confirmed drained playback followed by a later owner speech
      turn;
- [ ] DTMF PIN isolation from model output, tool arguments, transcript, and durable logs;
- [ ] structured approve, deny, instruct, defer, timeout, failure, and no-answer outcomes;
- [ ] bounded Codex task listing and inspection on inbound calls;
- [ ] a scoped Codex write with the default-off gate deliberately enabled;
- [ ] file-change callback denial;
- [ ] daemon restart or sideband loss during an active Realtime call, with both provider legs
      terminated and no tool-output replay;
- [ ] proof that the original MCP waiter socket does not masquerade as surviving restart;
- [ ] optional fallback expiry, lockout, one-time consumption, and no-action-authority rule;
- [ ] production reverse-proxy route allowlist.

## Release evidence

Attach or reference:

- the commit SHA and package version;
- sanitized `doctor --live` output;
- provider event IDs with phone numbers and secrets redacted;
- the durable Hotline event IDs;
- test and build command output;
- explicit notes for anything not exercised.

Never commit raw webhook bodies, provider headers, phone numbers, transcripts, PINs, API keys,
auth tokens, or the Hotline SQLite database.
