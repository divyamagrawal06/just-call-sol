# Verification record

Verified locally on 26 July 2026. This record is intentionally sanitized: it contains no
phone number, API key, callback URL, transcript, recording URL, or provider identifier.

## Live path

- The local daemon and its supervised Codex App Server child reported healthy.
- The public HTTPS health route reached the daemon through the demo tunnel.
- Sarvam Agent Studio version 2 was committed with all eight protected HTTP tools.
- The Sarvam deployment reconciler reported one active `inbound_outbound` deployment on
  version 2 with one bound number; a second reconciliation made no change.
- A real outbound call connected through the CLI path, completed 11 conversational
  messages over roughly 43 seconds, and the recipient confirmed that it worked.
- The completion callback reached the daemon. Because the conversation did not save an
  explicit decision through `record_decision`, the event closed without approval. This is
  the intended fail-closed behavior.
- No registered operational action or real cloud action was executed.

## Client path

- `agent-hotline@personal` is installed and enabled in Codex.
- Claude reports the `agent-hotline` stdio MCP server as connected.
- The native Codex App Server adapter completed initialization and thread-list smoke tests.
- The MCP subprocess suite completed a real initialize/list-tools/tool-call exchange.

## Local path

- The deterministic database-RU demo completed with a verified mock action and changed no
  real resource.
- The full automated suite, lint, package build, plugin validation, CLI smoke tests, and
  configured-secret scan are release gates. Exact final counts are recorded in the local
  commit handoff rather than hard-coded here.

## Deployment boundary

The current public endpoint uses a Cloudflare Quick Tunnel for demonstration. It has no
uptime guarantee and is not a production hosting setup. Production operation should use a
stable named tunnel or equivalent authenticated ingress, a service manager for the daemon,
credential rotation, and an explicitly registered runbook for each real infrastructure
action.
