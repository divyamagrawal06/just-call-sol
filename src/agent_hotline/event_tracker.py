"""Read-only navigation for the Sarvam Epoch registration demo CSV."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

DEFAULT_RELATIVE_PATH = Path(
    "outputs/sarvam_epoch_event_tracker/sarvam_epoch_registration_tracker.csv"
)
DEFAULT_SHEET_NAME = DEFAULT_RELATIVE_PATH.name
DEMO_SHEET_ALIASES = {"sheet_name.csv": DEFAULT_SHEET_NAME}


def _safe_sheet_name(sheet_name: str | None) -> str:
    requested = (sheet_name or DEFAULT_SHEET_NAME).strip().strip("\"'")
    requested = DEMO_SHEET_ALIASES.get(requested.casefold(), requested)
    candidate = Path(requested)
    if candidate.name != requested or candidate.suffix.casefold() != ".csv" or len(requested) > 200:
        raise ValueError("Only a CSV filename from the demo tracker directory is allowed.")
    return requested


def _candidate_paths(sheet_name: str | None = None) -> list[Path]:
    requested = _safe_sheet_name(sheet_name)
    configured = os.getenv("SARVAM_EPOCH_CSV")
    candidates = []
    if configured and Path(configured).name.casefold() == requested.casefold():
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            Path.cwd() / DEFAULT_RELATIVE_PATH.parent / requested,
            Path(__file__).resolve().parents[2] / DEFAULT_RELATIVE_PATH.parent / requested,
        ]
    )
    return candidates


def registration_csv_path(sheet_name: str | None = None) -> Path:
    for candidate in _candidate_paths(sheet_name):
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    searched = ", ".join(str(path) for path in _candidate_paths(sheet_name))
    raise FileNotFoundError(f"Sarvam Epoch registration CSV not found. Searched: {searched}")


def load_registrations(sheet_name: str | None = None) -> list[dict[str, str]]:
    with registration_csv_path(sheet_name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _compact_identifier(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _public_record(row: dict[str, str]) -> dict[str, str]:
    return {
        "registration_id": row["Registration ID"],
        "full_name": row["Full Name"],
        "ticket_type": row["Ticket Type"],
        "organization": row["Organization"],
        "approval_status": row["Approval Status"],
        "approval_reason": row["Approval Reason"],
        "check_in_status": row["Check-in Status"],
        "gate_note": row["Gate / Coordinator Note"],
    }


def check_registration(query: str, sheet_name: str | None = None) -> dict[str, Any]:
    """Find one participant in one allowlisted CSV and return registration evidence."""

    needle = _normalized(query)
    requested_sheet = (sheet_name or DEFAULT_SHEET_NAME).strip()
    try:
        resolved_sheet = registration_csv_path(sheet_name)
    except (FileNotFoundError, ValueError):
        return {
            "sheet_found": False,
            "sheet_name": requested_sheet,
            "found": False,
            "registered": False,
            "verdict": "HOLD",
            "approved": None,
            "reason": f"The requested CSV {requested_sheet!r} is not available.",
            "matches": [],
        }

    base_response = {
        "sheet_found": True,
        "sheet_name": resolved_sheet.name,
    }
    if not needle:
        return {
            **base_response,
            "found": False,
            "registered": False,
            "verdict": "HOLD",
            "approved": None,
            "reason": "A participant name, registration ID, email, or phone is required.",
            "matches": [],
        }

    rows = load_registrations(resolved_sheet.name)
    compact_needle = _compact_identifier(query)
    exact = [
        row
        for row in rows
        if _normalized(row["Email"]) == needle
        or _compact_identifier(row["Registration ID"]) == compact_needle
        or _compact_identifier(row["Phone"]) == compact_needle
    ]
    name_exact = [row for row in rows if _normalized(row["Full Name"]) == needle]
    matches = exact or name_exact

    if not matches:
        matches = [
            row
            for row in rows
            if needle in _normalized(row["Full Name"])
            or needle in _normalized(row["Email"])
            or compact_needle in _compact_identifier(row["Registration ID"])
            or compact_needle in _compact_identifier(row["Phone"])
        ]

    if not matches:
        return {
            **base_response,
            "found": False,
            "registered": False,
            "verdict": "HOLD",
            "approved": None,
            "reason": "No matching Sarvam Epoch registration was found.",
            "matches": [],
        }

    if len(matches) > 1:
        return {
            **base_response,
            "found": True,
            "registered": None,
            "verdict": "HOLD",
            "approved": None,
            "reason": (
                "Multiple registrations match. Use the registration ID to select the "
                "correct record before admitting the participant."
            ),
            "matches": [_public_record(row) for row in matches],
        }

    row = matches[0]
    status = row["Approval Status"].upper()
    if status == "APPROVED":
        verdict = "YES"
        approved: bool | None = True
    elif status in {"PENDING", "WAITLISTED"}:
        verdict = "HOLD"
        approved = None
    else:
        verdict = "NO"
        approved = False

    return {
        **base_response,
        "found": True,
        "registered": True,
        "verdict": verdict,
        "approved": approved,
        "approval_status": status,
        "reason": row["Approval Reason"],
        "gate_note": row["Gate / Coordinator Note"],
        "participant": _public_record(row),
    }


def search_registrations(query: str, limit: int = 10) -> dict[str, Any]:
    """Return compact matching records for participant disambiguation."""

    needle = _normalized(query)
    if not needle:
        return {"query": query, "count": 0, "matches": []}
    matches = [
        _public_record(row)
        for row in load_registrations()
        if any(
            needle in _normalized(row[field])
            for field in ("Registration ID", "Full Name", "Email", "Phone", "Organization")
        )
    ][: min(max(limit, 1), 25)]
    return {"query": query, "count": len(matches), "matches": matches}
