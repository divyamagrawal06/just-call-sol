"""Live, read-only terminal view of Codex tasks spawned by Better Call Sol."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".hotline/hotline.db"),
        help="Agent Hotline SQLite database (opened read-only).",
    )
    parser.add_argument("--poll", type=float, default=0.25)
    return parser


def _connect(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
        timeout=1.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _json_object(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _voice_action_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    payload = _json_object(row["payload_json"])
    target = payload.get("target")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    cwd = parameters.get("cwd")
    if not isinstance(cwd, str):
        return None
    if target == "thread.spawn_root":
        prompt = parameters.get("task")
        kind = "spawn"
        thread_id = None
    elif target == "thread.instruct":
        prompt = parameters.get("instruction")
        kind = "instruct"
        thread_id = parameters.get("thread_id")
    else:
        return None
    if not isinstance(prompt, str):
        return None
    return {
        "action_id": row["action_id"],
        "created_at": row["created_at"],
        "kind": kind,
        "prompt": " ".join(prompt.split()),
        "thread_id": thread_id,
        "cwd": cwd,
    }


def _terminal_result(connection: sqlite3.Connection, action_id: str) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT kind, details_json, occurred_at
        FROM timeline
        WHERE action_id = ?
          AND kind IN (
            'action_execution_succeeded',
            'action_execution_failed',
            'action_execution_unknown'
          )
        ORDER BY rowid DESC
        LIMIT 1
        """,
        (action_id,),
    ).fetchone()


def _short_workspace(cwd: str) -> str:
    return Path(cwd).name or cwd


def _print_request(payload: dict[str, Any], *, latest: bool = False) -> None:
    if payload["kind"] == "spawn":
        label = (
            "LATEST VOICE-SPAWNED CODEX TASK"
            if latest
            else "NEW VOICE-SPAWNED CODEX TASK"
        )
    else:
        label = "LATEST VOICE INSTRUCTION" if latest else "NEW VOICE INSTRUCTION"
    print("\n" + "=" * 78)
    print(label)
    print(f"Time:      {payload['created_at']}")
    print(f"Workspace: {_short_workspace(payload['cwd'])}")
    if isinstance(payload.get("thread_id"), str):
        print(f"Task ID:   {payload['thread_id']}")
    print(f"Prompt:    {payload['prompt']}")
    status = (
        "creating Codex task..."
        if payload["kind"] == "spawn"
        else "delivering to existing Codex task..."
    )
    print(f"Status:    {status}")
    print("=" * 78, flush=True)


def _print_result(row: sqlite3.Row) -> None:
    details = _json_object(row["details_json"])
    result = details.get("result")
    result = result if isinstance(result, dict) else {}
    if row["kind"] == "action_execution_succeeded":
        thread_id = result.get("thread_id")
        turn_id = result.get("turn_id")
        queued = result.get("turn_start_queued") is True
        action = result.get("action")
        if action == "spawned":
            print("\n[SPAWNED] Codex accepted the new task.")
        else:
            print("\n[DELIVERED] The existing Codex task accepted the instruction.")
        if isinstance(thread_id, str):
            print(f"Task ID:   {thread_id}")
            if action == "spawned":
                print(f"CLI ref:   codex://threads/{thread_id}")
        if isinstance(turn_id, str):
            print(f"Turn ID:   {turn_id}")
        if action == "steered":
            print("State:     active turn steered")
        elif queued:
            print("State:     first turn is starting in the background")
        else:
            print("State:     new turn started")
    else:
        message = details.get("message_to_user")
        print("\n[FAILED] Codex could not confirm the task creation.")
        if isinstance(message, str):
            print(f"Reason:    {' '.join(message.split())}")
    print(flush=True)


def _latest_action(
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], sqlite3.Row | None] | None:
    rows = connection.execute(
        """
        SELECT rowid, action_id, created_at, payload_json
        FROM prepared_actions
        ORDER BY rowid DESC
        LIMIT 100
        """
    ).fetchall()
    for row in rows:
        payload = _voice_action_payload(row)
        if payload is not None:
            return payload, _terminal_result(connection, payload["action_id"])
    return None


def watch(database: Path, poll_seconds: float) -> int:
    if poll_seconds <= 0:
        raise ValueError("--poll must be positive")
    connection = _connect(database)
    action_cursor = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM prepared_actions"
    ).fetchone()[0]
    timeline_cursor = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM timeline"
    ).fetchone()[0]
    tracked: dict[str, dict[str, Any]] = {}

    print("Better Call Sol — live Codex task feed")
    print(f"Database: {database.resolve()}")
    print("READY. New voice task requests will appear here immediately.", flush=True)
    latest = _latest_action(connection)
    if latest is not None:
        payload, terminal = latest
        _print_request(payload, latest=True)
        if terminal is not None:
            _print_result(terminal)

    while True:
        try:
            action_rows = connection.execute(
                """
                SELECT rowid, action_id, created_at, payload_json
                FROM prepared_actions
                WHERE rowid > ?
                ORDER BY rowid
                """,
                (action_cursor,),
            ).fetchall()
            for row in action_rows:
                action_cursor = max(action_cursor, row["rowid"])
                payload = _voice_action_payload(row)
                if payload is None:
                    continue
                tracked[payload["action_id"]] = payload
                _print_request(payload)

            timeline_rows = connection.execute(
                """
                SELECT rowid, action_id, kind, details_json, occurred_at
                FROM timeline
                WHERE rowid > ?
                ORDER BY rowid
                """,
                (timeline_cursor,),
            ).fetchall()
            for row in timeline_rows:
                timeline_cursor = max(timeline_cursor, row["rowid"])
                action_id = row["action_id"]
                if action_id not in tracked:
                    continue
                if row["kind"] in {
                    "action_execution_succeeded",
                    "action_execution_failed",
                    "action_execution_unknown",
                }:
                    _print_result(row)
                    tracked.pop(action_id, None)
        except sqlite3.OperationalError as exc:
            print(f"[waiting for database] {type(exc).__name__}", flush=True)
            connection.close()
            time.sleep(max(poll_seconds, 1.0))
            connection = _connect(database)
        time.sleep(poll_seconds)


def main() -> int:
    args = _parser().parse_args()
    try:
        return watch(args.database, args.poll)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
