"""Tests for the CSV-backed Sarvam Epoch demo tools."""

from __future__ import annotations

from agent_hotline.event_tracker import check_registration, search_registrations


def test_approved_registration_returns_yes() -> None:
    result = check_registration("SEP-26001")
    assert result["verdict"] == "YES"
    assert result["approved"] is True
    assert result["participant"]["full_name"] == "Aarav Sharma"


def test_rejected_registration_returns_no_and_reason() -> None:
    result = check_registration("kabir.malhotra@epoch-attendee.example")
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


def test_search_supports_partial_organization() -> None:
    result = search_registrations("cloudcraft")
    assert result["count"] == 2
    assert {row["registration_id"] for row in result["matches"]} == {
        "SEP-26007",
        "SEP-26019",
    }
