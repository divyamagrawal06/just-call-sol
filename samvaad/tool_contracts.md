# Samvaad HTTP tool contracts

All tool calls use:

```http
Authorization: Bearer <HOTLINE_TOOL_TOKEN>
Content-Type: application/json
```

The bearer token is stored in Agent Studio's secret/tool configuration. It is never an agent
variable and is never spoken or returned. The owner PIN is also never an agent variable,
response mapping, transcript note, or log field. It exists only ephemerally in the
`confirmation_pin` request field. Never send an `identity_verified` field: the daemon derives
that fact from a constant-time PIN comparison.

## `begin_inbound`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/begin-inbound

{
  "caller_phone_number": "<provider caller number>",
  "interaction_id": "<interaction id>"
}
```

`interaction_id` is required and must identify the current provider interaction. An
accepted response means that interaction was correlated and the caller was allowlisted. It
never means identity-verified.

## `get_context`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/context

{
  "event_id": "<event_id>",
  "detail_level": "standard"
}
```

Run: during conversation, before factual claims.

## `record_decision`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/record-instruction

{
  "event_id": "<event_id>",
  "outcome": "<dynamic: approve|deny|instruct|defer|auth_completed>",
  "instruction": "<dynamic confirmed instruction, including any constraints>",
  "constraints": [],
  "approved_action_ids": [],
  "confirmation_method": "spoken_plus_dtmf",
  "confirmation_pin": "<dynamic ephemeral owner DTMF PIN>"
}
```

Run: during conversation, immediately after clear readback and confirmation. This is the
authoritative decision path that wakes the waiting MCP call. The live tool varies only
`outcome`, `instruction`, and `confirmation_pin`; `constraints` and `approved_action_ids`
remain empty, and `confirmation_method` remains `spoken_plus_dtmf`. Fold ordinary
constraints into the confirmed instruction. Every outcome requires the DTMF PIN because
each one wakes the waiting agent. The PIN must be 6–12 ASCII digits.

An ordinary Codex command/file approval is represented by `outcome: "approve"` plus the
daemon-verified PIN and no action ID. A registered action is different: its authority comes
only from `confirm_action` and its one-time grant, and its execution is audited by
`execute_action`. Never try to grant a registered action through `approved_action_ids` in
the live `record_decision` tool.

## `prepare_action`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/prepare-action

{
  "event_id": "<event_id>",
  "action_type": "<agent-decided allowlisted action>",
  "action_reference": "<agent-decided task reference, or empty>",
  "action_instruction": "<agent-decided instruction, or empty>",
  "action_turn_id": "<agent-decided active turn id, or empty>",
  "action_task": "<agent-decided new root task, or empty>",
  "action_cwd": ".",
  "action_confirmed_thread_id": "<agent-decided exact inspected task id, or empty>",
  "action_target_ru": "<agent-decided integer, or 0>",
  "action_pause_reason": "<agent-decided enum, or empty>"
}
```

Run: during conversation, before any medium/high-risk confirmation.

In Agent Studio configure `event_id` as **Agent variable**. Configure `action_type`,
`action_reference`, `action_instruction`, `action_turn_id`, `action_task`,
`action_confirmed_thread_id`, `action_target_ru`, and `action_pause_reason` as **Let the
agent decide**. Configure `action_cwd` as the **Fixed value** `"."`; the model must never
select a filesystem path. Do not add `parameters`, `workspace_ref`, `thread_id`, or
`commit_or_state_hash` fields. The daemon binds workspace/thread scope from the live event
and maps only these typed fields:

- `thread.instruct`: `action_reference` and `action_instruction` are required.
- `thread.interrupt`: `action_reference` is required; `action_turn_id` is optional.
- `thread.spawn_root`: `action_task` is required and `action_cwd` stays fixed to `"."`.
- `thread.archive`: `action_reference` and `action_confirmed_thread_id` are required.
- `demo.increase_db_ru_limit`: `action_target_ru` is a required integer from 401–10000.
- `demo.pause_deployment`: `action_pause_reason` is optional and restricted to
  `owner-request`, `incident-response`, or `demo`.
- `demo.terminate_batch_runs`: has no agent-decided parameters.

Send empty strings for non-integer fields that do not apply to the selected action, and use
`0` for an unused `action_target_ru`. The request model turns those sentinel slots into
absence, rejects cross-action fields and arbitrary keys, and
then passes the normalized `parameters` object to the existing strict coordinator/runbook
validation. This flat adapter exists because Agent Studio binds HTTP request fields
individually; it does not grant the model an arbitrary JSON object or a command surface.
The exact readback, DTMF PIN, `confirm_action`, and one-time grant remain mandatory.

Demo-only exception: when `HOTLINE_DEMO_AUTO_EXECUTE_ACTIONS=true` outside production,
`prepare_action` internally records a `trusted_local` grant, consumes it through the normal
audit path, and executes immediately. The response sets `executed: true` and returns
`message_to_user`, `operation_id`, and `result`; an identical retry in the same event and
provider-correlated call sets `already_executed: true` without executing twice. The flag
defaults to false, production rejects it, and it cannot be combined with
`HOTLINE_ALLOW_REAL_ACTIONS`.

## `confirm_action`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/confirm-action

{
  "event_id": "<event_id>",
  "action_id": "<prepared action id>",
  "confirmation_nonce": "<nonce>",
  "exact_confirmation": "<caller phrase>",
  "confirmation_method": "spoken_plus_dtmf",
  "confirmation_pin": "<ephemeral owner DTMF PIN>"
}
```

Run: only after exact readback and DTMF collection. `confirmed: true` means the daemon matched
the configured PIN; the model must never make that claim itself.

## `execute_action`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/execute-action

{
  "event_id": "<event_id>",
  "action_id": "<prepared action id>",
  "grant_id": "<confirmed one-time grant id>"
}
```

Run only after `confirm_action` returns `confirmed: true`. The one-time grant—not
`record_decision`—authorizes this exact registered action. A separate `record_decision`
call may wake a waiting agent with the action outcome, but it must keep
`approved_action_ids` empty.

## `list_threads`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/threads/list

{
  "event_id": "<provider-correlated live inbound or outbound event id>",
  "query": "<optional name, preview, workspace, or status; running means active>",
  "limit": 10
}
```

Inbound events must come from a successful `begin_inbound`; outbound events must carry the
provider attempt linked when the daemon dialed. The session must still be live. This
read-only result does not verify identity or authorize a decision or mutation.

## `inspect_thread`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/threads/inspect

{
  "event_id": "<provider-correlated live inbound or outbound event id>",
  "reference": "<unambiguous task reference>"
}
```

The same live-session rules as `list_threads` apply.

## `repo_context`

```http
POST <PUBLIC_BASE_URL>/v1/sarvam/tools/repository-context

{
  "event_id": "<live inbound or outbound event id>",
  "confirmation_pin": "<dynamic ephemeral owner DTMF PIN>",
  "workspace": "<allowlisted workspace label/ref, or empty when unambiguous>",
  "operation": "<agent-decided: status|diff|search|read|tests>",
  "query": "<agent-decided literal search text, otherwise empty>",
  "path": "<agent-decided relative repository path, otherwise empty>",
  "line_start": "<agent-decided integer read offset; use 1 otherwise>",
  "line_count": 40,
  "max_results": 10
}
```

In Agent Studio configure `event_id` as **Agent variable**. Configure `workspace` as a
**Fixed value** of empty string for the single allowlisted demo root. Configure `operation`,
`query`, `path`, `line_start`, and `confirmation_pin` as **Let the agent decide**; operation
is restricted by the daemon to `status|diff|search|read|tests`, query is used only by search,
path is repository-relative, and line_start is a backend-bounded integer used for read
pagination. Configure `line_count=40` and `max_results=10` as **Fixed value**. Do not create
response-variable mappings; preserve the raw JSON response for conversational reasoning.

Collect the PIN by DTMF immediately before the query and pass it only in
`confirmation_pin`. Never repeat it, save it as an agent/input/output variable, or include
it in a response mapping or transcript note. The route requires the HTTP tool bearer token,
the daemon-verified PIN, and a direction-matched, provider-correlated live call session.
Outbound sessions must carry the persisted provider `attempt_id`; queries are forced to
the workspace recorded on that escalation and fail closed if it is absent. Inbound
sessions must carry the persisted provider `interaction_id`, be in `CONNECTED` or
`DISCUSSING`, belong to an allowlisted inbound event, and can select only roots in
`HOTLINE_WORKSPACE_ROOTS`.
`read` is capped at 80 lines; `search` is literal and bounded; `status` and `diff` use fixed
Git argument vectors; `tests` reports a static inventory and never executes tests or claims
pass/fail. All returned text and metadata are redacted, size-limited, and marked as
untrusted evidence.

As soon as the daemon accepts an authenticated, schema-valid request, it durably marks the
event evidence-only before attempting the filesystem or Git query. This remains true if the
query fails or returns no evidence. Do not call `record_decision`, `prepare_action`,
`confirm_action`, or `execute_action`; the daemon rejects them even with a correct PIN or an
earlier grant. Tell the owner that an authority-bearing decision or action requires a fresh
call that does not load repository evidence.

The exact source-manifest tool names for the next committed app version are `begin_inbound`,
`get_context`, `record_decision`,
`prepare_action`, `confirm_action`, `execute_action`, `list_threads`, `inspect_thread`, and
`repo_context`.
