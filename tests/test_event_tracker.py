"""Tests for the CSV-backed Sarvam Epoch demo tools."""

from __future__ import annotations

from agent_hotline.event_tracker import check_registration, search_registrations


def test_approved_registration_returns_yes() -> None:
    result = check_registration("SEP-26001")
    assert result["registered"] is True
    assert result["verdict"] == "YES"
    assert result["approved"] is True
    assert result["participant"]["full_name"] == "Aarav Sharma"


def test_rejected_registration_returns_no_and_reason() -> None:
    result = check_registration("kabir.malhotra@epoch-attendee.example")
    assert result["registered"] is True
    assert result["verdict"] == "NO"
    assert result["approved"] is False
    assert "submitted ID does not match" in result["reason"]


def test_duplicate_name_requires_registration_id() -> None:
    result = check_registration("Rohan Mehta")
    assert result["verdict"] == "HOLD"
    assert result["approved"] is None
    assert len(result["matches"]) == 2


def test_pending_registration_returns_hold() -> None:
    result = check_registration("SEP-26005")
    assert result["verdict"] == "HOLD"
    assert result["approved"] is None


def test_spoken_registration_id_and_demo_sheet_alias_work() -> None:
    result = check_registration("SEP 26003", "sheet_name.csv")
    assert result["sheet_found"] is True
    assert result["sheet_name"] == "sarvam_epoch_registration_tracker.csv"
    assert result["registered"] is True
    assert result["approval_status"] == "REJECTED"


def test_unknown_sheet_is_a_structured_hold() -> None:
    result = check_registration("SEP-26001", "missing.csv")
    assert result["sheet_found"] is False
    assert result["registered"] is False
    assert result["verdict"] == "HOLD"


def test_search_supports_partial_organization() -> None:
    result = search_registrations("cloudcraft")
    assert result["count"] == 2
    assert {row["registration_id"] for row in result["matches"]} == {
        "SEP-26007",
        "SEP-26019",
    }
