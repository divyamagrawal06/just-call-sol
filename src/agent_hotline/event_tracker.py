"""Read-only navigation for the Sarvam Epoch registration demo CSV."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any

DEFAULT_RELATIVE_PATH = Path(
    "outputs/sarvam_epoch_event_tracker/sarvam_epoch_registration_tracker.csv"
)


def _candidate_paths() -> list[Path]:
    configured = os.getenv("SARVAM_EPOCH_CSV")
    candidates = [Path(configured).expanduser()] if configured else []
    candidates.extend(
        [
            Path.cwd() / DEFAULT_RELATIVE_PATH,
            Path(__file__).resolve().parents[2] / DEFAULT_RELATIVE_PATH,
        ]
    )
    return candidates


def registration_csv_path() -> Path:
    for candidate in _candidate_paths():
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    searched = ", ".join(str(path) for path in _candidate_paths())
    raise FileNotFoundError(f"Sarvam Epoch registration CSV not found. Searched: {searched}")


def load_registrations() -> list[dict[str, str]]:
    with registration_csv_path().open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _normalized(value: str) -> str:
    return " ".join(value.casefold().strip().split())


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


def check_registration(query: str) -> dict[str, Any]:
    """Find one participant and return an admission verdict with evidence."""

    needle = _normalized(query)
    if not needle:
        return {
            "found": False,
            "verdict": "HOLD",
            "approved": None,
            "reason": "A participant name, registration ID, email, or phone is required.",
            "matches": [],
        }

    rows = load_registrations()
    exact_fields = ("Registration ID", "Email", "Phone")
    exact = [
        row for row in rows if any(_normalized(row[field]) == needle for field in exact_fields)
    ]
    name_exact = [row for row in rows if _normalized(row["Full Name"]) == needle]
    matches = exact or name_exact

    if not matches:
        matches = [
            row
            for row in rows
            if needle in _normalized(row["Full Name"])
            or needle in _normalized(row["Registration ID"])
            or needle in _normalized(row["Email"])
            or needle in _normalized(row["Phone"])
        ]

    if not matches:
        return {
            "found": False,
            "verdict": "NO",
            "approved": False,
            "reason": "No matching Sarvam Epoch registration was found.",
            "matches": [],
        }

    if len(matches) > 1:
        return {
            "found": True,
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
        "found": True,
        "verdict": verdict,
        "approved": approved,
        "reason": row["Approval Reason"],
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
