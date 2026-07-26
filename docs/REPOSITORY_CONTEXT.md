# Repository context capability

## What the voice agent could access before this change

`get_context` returned only the compact `ContextPacket` saved when an escalation was
created: task/agent summaries, branch/commit fields supplied by the caller, diff/test/error
summaries supplied by the caller, constraints, and evidence references. It did not refresh
those facts from disk.

For an allowlisted inbound call, `list_threads` and `inspect_thread` returned bounded Codex
App Server metadata: task ID/name/preview/status/workspace basename and recent turn IDs and
states. They did not return source files, Git status, diffs, or test results. Claude had no
equivalent deep task reader; its hooks only submitted sanitized hook payloads.

The shared Codex/Claude MCP surface could create/wait for calls, request an authentication
handoff, list Hotline events, and check daemon health. It had no repository query tool.

## Source capability after this change

Both clients now receive the MCP tool `query_repository_context`, backed by:

```text
POST /v1/repository/context
Authorization: Bearer <HOTLINE_LOCAL_TOKEN>
```

The generated Samvaad manifest now includes `repo_context`, backed by:

```text
POST /v1/sarvam/tools/repository-context
Authorization: Bearer <HOTLINE_TOOL_TOKEN>
```

The public route additionally requires an existing live call event and a fresh ephemeral
owner PIN that the daemon verifies. An outbound query requires the persisted provider
`attempt_id`, is forced to that event's workspace, and fails closed when the event lacks
one. An inbound query requires the event created by a successfully allowlisted inbound
caller plus a session carrying the persisted provider `interaction_id` in `CONNECTED` or
`DISCUSSING`; it may select only a configured root. Direction/state/correlation validation
precedes the constant-time PIN comparison, and the route permits at most six attempts per
minute per client.

Once an authenticated, schema-valid request is accepted, the daemon durably marks that
event evidence-only before attempting the filesystem or Git query. `record_decision`,
`prepare_action`, `confirm_action`, and `execute_action` then fail closed for that event,
even with the correct PIN or a previously issued grant. This prevents repository-controlled
text from sharing a model context with an authority-bearing tool call. The owner must use a
fresh confirmation call that does not load repository evidence. The marker is intentionally
written before the query, so the event remains evidence-only even if the requested path
cannot be read or the query returns no evidence.

The five operations are:

| Operation | Evidence returned | Important limitation |
| --- | --- | --- |
| `status` | Branch header and bounded changed/untracked entries | Fixed Git status only |
| `diff` | Staged/unstaged diff stats and untracked names | Summary, not an arbitrary patch command |
| `search` | Case-insensitive literal matches with relative path/line | 20 results, 2,000 files, 8 MiB scan |
| `read` | Numbered UTF-8 text chunks | Relative path, 80 lines, 256 KiB file |
| `tests` | Static test-file/case inventory and changed-test count | Does not run tests or claim pass/fail |

All operations are read-only. Search/read use filesystem APIs. Status/diff use only fixed
Git argument vectors with `shell=False`, prompts and optional locks disabled, external
diff/text conversion disabled, and a five-second timeout. There is no `command`, `args`,
regular-expression, URL-fetch, write, or execute field.

## Workspace and disclosure boundary

Set exact roots with a semicolon-separated value:

```dotenv
HOTLINE_WORKSPACE_ROOTS=C:\work\repo-a;C:\work\repo-b
```

If unset, the service exposes only an explicitly configured `CODEX_APP_SERVER_CWD` that is a
Git root; with neither setting it fails closed. It never falls back to the process cwd or
user home. With multiple roots, callers must use the returned opaque `workspace_ref` or an
unambiguous root label.

The service rejects:

- absolute paths, `..` traversal, and paths outside the selected root;
- symlink, Windows junction, and other reparse-directory escapes;
- Gitfiles and linked-worktree/submodule roots; `.git` must be a real local directory
  directly under the allowlisted root;
- `.git`, `.hotline`, virtual environments, dependencies, build output, and caches;
- `.env`, common credential filenames, private-key/keystore suffixes;
- binary, non-UTF-8, unsupported-extension, and files larger than 256 KiB.

Workspace labels, relative paths, summaries, and evidence text are sanitized. Known runtime
secrets and phone-number patterns are redacted; control characters, bidi/zero-width text,
role delimiters, and common instruction-injection phrases are neutralized. The complete
serialized response is capped at 6,000 characters and carries `untrusted_data: true`.
Neutralization is defense in depth, not a claim that arbitrary repository text is safe.

## Remaining gaps and operational step

- The static test inventory cannot establish whether tests passed. Pass/fail remains an
  agent-supplied `ContextPacket.test_summary` until a separate signed test-result collector
  is implemented.
- Search intentionally does not offer regular expressions or semantic indexing.
- Git evidence is local working-tree state only; there is no remote PR/CI/provider lookup.
- The repository service does not add Claude task enumeration; Claude and Codex share the
  MCP repository query, while deep task control remains Codex App Server-specific.
- A running daemon must be restarted to load the new route.
- The already deployed Samvaad app does not gain `repo_context` from a source change.
  Configure the ninth tool from `agent-hotline-sarvam tools-manifest`, update the prompt and
  variables, commit a new Agent Studio version, update `SARVAM_APP_VERSION`, and reconcile
  the deployment. This document does not claim that live mutation has occurred.
