from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from agent_hotline.settings import Settings

_OPENAI_READY_VALUES: dict[str, object] = {
    "openai_api_key": "sk-openai-ready-sentinel",
    "openai_webhook_secret": "whsec-openai-ready-sentinel",
    "openai_project_id": "proj_ready123",
    "public_base_url": "https://hotline.example.test",
    "hotline_sip_correlation_secret": "correlation-ready-sentinel-1234567890",
    "hotline_action_signing_secret": "action-ready-sentinel-123456789012",
    "owner_confirmation_pin": "123456",
}

_TWILIO_READY_VALUES: dict[str, object] = {
    # Construct the valid test shape without committing a credential-shaped literal.
    "twilio_account_sid": "AC" + "0123456789abcdef" * 2,
    "twilio_auth_token": "twilio-auth-ready-sentinel",
    "twilio_phone_number": "+12025550124",
    "owner_phone_number": "+12025550123",
    "openai_project_id": "proj_ready123",
    "public_base_url": "https://hotline.example.test",
    "hotline_sip_correlation_secret": "correlation-ready-sentinel-1234567890",
}

_LOCAL_READY_VALUES: dict[str, object] = {
    "hotline_local_token": "local-ready-sentinel-12345678901234",
}


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("hotline_transport", "openai_realtime"),
        ("openai_realtime_model", "gpt-realtime-2.1"),
        ("openai_realtime_voice", "marin"),
        ("openai_realtime_reasoning_effort", "low"),
        ("twilio_bridge_mode", "sip"),
    ],
)
def test_openai_realtime_defaults_are_the_production_profile(
    field: str,
    expected: str,
) -> None:
    assert getattr(_settings(), field) == expected


def test_settings_honor_environment_values_set_inside_a_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-explicit-test-value")

    settings = _settings()

    assert settings.openai_realtime_model == "gpt-realtime-2"
    assert settings.openai_api_key.get_secret_value() == "sk-explicit-test-value"


def test_openai_realtime_provider_is_configured_when_its_prerequisites_are_present() -> None:
    assert _settings(**_OPENAI_READY_VALUES).openai_realtime_configured is True
    assert _settings(**_OPENAI_READY_VALUES).openai_realtime_runtime_ready is False


def test_openai_realtime_runtime_is_ready_with_openai_and_twilio_prerequisites() -> None:
    values = {
        **_OPENAI_READY_VALUES,
        **_TWILIO_READY_VALUES,
        **_LOCAL_READY_VALUES,
    }

    assert _settings(**values).openai_realtime_runtime_ready is True


def test_media_stream_mode_does_not_require_an_openai_webhook_secret() -> None:
    values = {
        **_OPENAI_READY_VALUES,
        **_TWILIO_READY_VALUES,
        **_LOCAL_READY_VALUES,
        "openai_webhook_secret": "",
        "twilio_bridge_mode": "media_stream",
    }

    assert _settings(**values).openai_realtime_runtime_ready is True


def test_openai_realtime_allows_only_one_active_call() -> None:
    assert _settings().hotline_max_active_calls == 1
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        _settings(hotline_max_active_calls=2)


@pytest.mark.parametrize("missing", tuple(_OPENAI_READY_VALUES))
def test_openai_realtime_is_not_configured_when_a_prerequisite_is_missing(
    missing: str,
) -> None:
    values = {**_OPENAI_READY_VALUES, missing: ""}

    assert _settings(**values).openai_realtime_configured is False
    assert _settings(**values).openai_realtime_runtime_ready is False


def test_twilio_is_configured_when_every_prerequisite_is_present() -> None:
    assert _settings(**_TWILIO_READY_VALUES).twilio_configured is True


@pytest.mark.parametrize("missing", tuple(_TWILIO_READY_VALUES))
def test_twilio_is_not_configured_when_a_prerequisite_is_missing(missing: str) -> None:
    values = {**_TWILIO_READY_VALUES, missing: ""}

    assert _settings(**values).twilio_configured is False


def test_diagnostics_report_presence_without_exposing_configuration_values() -> None:
    values = {
        **_OPENAI_READY_VALUES,
        **_TWILIO_READY_VALUES,
        "openai_api_key": "sk-diagnostics-secret-sentinel",
        "openai_webhook_secret": "whsec-diagnostics-secret-sentinel",
        "openai_project_id": "proj_diagnostics_sentinel",
        "twilio_account_sid": "AC" + "deadbeef" * 4,
        "twilio_auth_token": "twilio-diagnostics-secret-sentinel",
        "twilio_phone_number": "+12025550991",
        "owner_phone_number": "+12025550992",
        "owner_confirmation_pin": "998877",
        "hotline_local_token": "local-diagnostics-secret-sentinel-123456",
        "hotline_sip_correlation_secret": ("correlation-diagnostics-secret-sentinel-123456"),
        "hotline_action_signing_secret": ("action-diagnostics-secret-sentinel-123456"),
        "hotline_fallback_signing_secret": ("fallback-diagnostics-secret-sentinel-123456"),
        "public_base_url": "https://diagnostics-private.example.test",
    }
    settings = _settings(**values)

    diagnostics = settings.diagnostics()
    serialized = json.dumps(diagnostics, sort_keys=True)

    for value in values.values():
        if isinstance(value, str) and value not in {
            settings.openai_realtime_model,
            settings.openai_realtime_voice,
            settings.openai_realtime_reasoning_effort,
        }:
            assert value not in serialized

    assert diagnostics["openai_api_key_configured"] is True
    assert diagnostics["openai_webhook_secret_configured"] is True
    assert diagnostics["openai_project_configured"] is True
    assert diagnostics["sip_correlation_secret_configured"] is True
    assert diagnostics["action_signing_secret_configured"] is True
    assert diagnostics["fallback_signing_secret_configured"] is True
    assert diagnostics["openai_realtime_configured"] is True
    assert diagnostics["openai_realtime_runtime_ready"] is True
    assert diagnostics["twilio_account_configured"] is True
    assert diagnostics["twilio_auth_token_configured"] is True
    assert diagnostics["twilio_number_configured"] is True
    assert diagnostics["twilio_configured"] is True


def test_openai_sip_uri_is_derived_only_from_the_project_id() -> None:
    settings = _settings(
        openai_project_id="proj_sip123",
        openai_api_key="sk-must-not-appear",
    )

    assert settings.openai_sip_uri == ("sip:proj_sip123@sip.api.openai.com;transport=tls")
    assert "sk-must-not-appear" not in settings.openai_sip_uri
    assert _settings().openai_sip_uri is None


@pytest.mark.parametrize(
    "project_id",
    [
        "project_example",
        "proj space",
        "sip:proj_example@sip.api.openai.com",
        "proj_/path",
    ],
)
def test_openai_project_id_rejects_values_that_cannot_form_a_safe_sip_uri(
    project_id: str,
) -> None:
    with pytest.raises(ValidationError, match="OPENAI_PROJECT_ID"):
        _settings(openai_project_id=project_id)


@pytest.mark.parametrize(
    "overrides",
    [
        {"openai_realtime_model": "gpt-4.1"},
        {"openai_realtime_model": "gpt-realtime-1.5"},
        {"openai_realtime_voice": "unknown-voice"},
        {"openai_realtime_reasoning_effort": "unbounded"},
    ],
)
def test_openai_session_profile_rejects_unsupported_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        _settings(**overrides)


@pytest.mark.parametrize("pin", ["123456", "0" * 12])
def test_owner_confirmation_pin_accepts_ascii_digit_boundaries(pin: str) -> None:
    assert _settings(owner_confirmation_pin=pin).owner_confirmation_pin.get_secret_value() == pin


@pytest.mark.parametrize(
    "pin",
    [
        "12345",
        "1" * 13,
        "12345a",
        "\uff11\uff12\uff13\uff14\uff15\uff16",
    ],
)
def test_owner_confirmation_pin_rejects_invalid_values(pin: str) -> None:
    with pytest.raises(ValidationError, match="OWNER_CONFIRMATION_PIN"):
        _settings(owner_confirmation_pin=pin)


def test_empty_owner_number_is_allowed_for_unconfigured_installations() -> None:
    assert _settings().owner_phone_number.get_secret_value() == ""


def test_owner_number_accepts_strict_e164() -> None:
    settings = _settings(owner_phone_number="+12025550123")

    assert settings.owner_phone_number.get_secret_value() == "+12025550123"


@pytest.mark.parametrize(
    "phone_number",
    [
        "12025550123",
        "+1 202 555 0123",
        "+0123456789",
        "+1234567",
        "+" + ("1" * 16),
    ],
)
def test_nonempty_owner_number_must_be_strict_e164(phone_number: str) -> None:
    with pytest.raises(ValidationError, match=r"OWNER_PHONE_NUMBER.*E\.164"):
        _settings(owner_phone_number=phone_number)
