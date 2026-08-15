"""Runtime configuration with secret-safe diagnostics."""

from __future__ import annotations

import os
import platform
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WINDOWS_USER_ENV_KEYS = (
    "OPENAI_API_KEY",
    "OPENAI_WEBHOOK_SECRET",
    "OPENAI_PROJECT_ID",
    "OPENAI_REALTIME_MODEL",
    "OPENAI_REALTIME_VOICE",
    "OPENAI_REALTIME_REASONING_EFFORT",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_PHONE_NUMBER",
    "VOBIZ_AUTH_ID",
    "VOBIZ_AUTH_TOKEN",
    "VOBIZ_PHONE_NUMBER",
    "VAPI_WEBHOOK_TOKEN",
    "VAPI_ASSISTANT_ID",
    "VAPI_PHONE_NUMBER_ID",
    "OWNER_PHONE_NUMBER",
    "OWNER_CONFIRMATION_PIN",
    "HOTLINE_OWNER_NAME",
    "HOTLINE_VOICE_PIN_MAX_ATTEMPTS",
    "HOTLINE_LOCAL_TOKEN",
    "HOTLINE_SIP_CORRELATION_SECRET",
    "HOTLINE_ACTION_SIGNING_SECRET",
    "HOTLINE_FALLBACK_SIGNING_SECRET",
    "HOTLINE_FALLBACK_WEBHOOK_URL",
    "HOTLINE_FALLBACK_WEBHOOK_TOKEN",
    "HOTLINE_FALLBACK_TTL_SECONDS",
    "HOTLINE_FALLBACK_MAX_PIN_ATTEMPTS",
    "HOTLINE_GIT_BIN",
    "HOTLINE_WORKSPACE_ROOTS",
    "HOTLINE_TRANSPORT",
    "HOTLINE_CARRIER",
    "HOTLINE_ALLOW_CODEX_WRITES",
    "HOTLINE_DEMO_AUTO_EXECUTE_ACTIONS",
    "PUBLIC_BASE_URL",
)


def hydrate_windows_user_environment() -> None:
    """Load selected user-scoped variables into this process on Windows.

    ``setx`` updates the user environment for future processes, but a long-running
    Codex host does not automatically receive the broadcast. Reading only the
    explicit allowlist avoids importing unrelated user secrets.
    """

    if platform.system() != "Windows":
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in _WINDOWS_USER_ENV_KEYS:
                if name in os.environ:
                    continue
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if isinstance(value, str) and value:
                    os.environ[name] = value
    except OSError:
        # A locked-down host may deny registry reads. Normal environment and .env
        # loading still work.
        return


hydrate_windows_user_environment()


def runtime_env_path() -> Path:
    """Return the stable per-user secret file used outside Windows registry storage."""

    configured_root = os.environ.get("XDG_CONFIG_HOME")
    if configured_root and Path(configured_root).is_absolute():
        config_root = Path(configured_root)
    elif platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        config_root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    else:
        config_root = Path.home() / ".config"
    return config_root / "agent-hotline" / "runtime.env"


class Settings(BaseSettings):
    """Configuration for the daemon, MCP bridge, and provider client."""

    model_config = SettingsConfigDict(
        env_file=(str(runtime_env_path()), ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        case_sensitive=False,
    )

    hotline_env: Literal["development", "test", "production"] = "development"
    hotline_host: str = "127.0.0.1"
    hotline_port: int = Field(default=8787, ge=1, le=65535)
    hotline_database_path: Path = Path(".hotline/hotline.db")
    hotline_daemon_url: str = "http://127.0.0.1:8787"
    hotline_local_token: SecretStr = SecretStr("")
    hotline_sip_correlation_secret: SecretStr = SecretStr("")
    hotline_action_signing_secret: SecretStr = SecretStr("")
    hotline_fallback_signing_secret: SecretStr = SecretStr("")
    hotline_fallback_webhook_url: str | None = None
    hotline_fallback_webhook_token: SecretStr = SecretStr("")
    hotline_fallback_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    hotline_fallback_max_pin_attempts: int = Field(default=5, ge=1, le=10)
    hotline_decision_timeout_seconds: int = Field(default=600, ge=1, le=1200)
    hotline_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    public_base_url: str | None = None

    # OpenAI Realtime is the conversational runtime. The selected SIP carrier
    # originates PSTN calls and forwards both inbound and outbound legs to the
    # project-scoped OpenAI SIP endpoint.
    openai_api_key: SecretStr = SecretStr("")
    openai_webhook_secret: SecretStr = SecretStr("")
    openai_project_id: str | None = None
    openai_realtime_model: str = "gpt-realtime-2.1"
    openai_realtime_voice: Literal[
        "alloy",
        "ash",
        "ballad",
        "coral",
        "echo",
        "sage",
        "shimmer",
        "verse",
        "marin",
        "cedar",
    ] = "marin"
    openai_realtime_reasoning_effort: Literal[
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
    ] = "low"

    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_phone_number: str | None = None
    twilio_bridge_mode: Literal["sip", "media_stream"] = "sip"

    hotline_carrier: Literal["twilio", "vobiz"] = "twilio"
    vobiz_auth_id: str | None = None
    vobiz_auth_token: SecretStr = SecretStr("")
    vobiz_phone_number: str | None = None

    # Vapi owns the conversational/PSTN path while this daemon remains the
    # authenticated, allowlisted Codex task-control backend.
    vapi_webhook_token: SecretStr = SecretStr("")
    vapi_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None

    owner_phone_number: SecretStr = SecretStr("")
    owner_confirmation_pin: SecretStr = SecretStr("")
    hotline_owner_name: str = Field(default="Owner", min_length=1, max_length=80)
    hotline_voice_pin_max_attempts: int = Field(default=3, ge=1, le=10)

    codex_bin: str = "codex"
    codex_app_server_enabled: bool = True
    codex_app_server_cwd: Path | None = None
    hotline_show_spawned_codex_tasks: bool = False
    # Semicolon-separated repository roots. An empty value safely falls back to
    # the daemon's single configured Codex cwd, not to the user's home directory.
    hotline_workspace_roots: str = ""
    hotline_git_bin: Path | None = None

    hotline_transport: Literal["openai_realtime", "fake", "disabled"] = "openai_realtime"
    hotline_allow_codex_writes: bool = False
    hotline_demo_auto_execute_actions: bool = False
    hotline_allow_real_runbooks: bool = False
    hotline_allowlisted_callers: str = ""
    hotline_max_active_calls: int = Field(default=1, ge=1, le=1)
    hotline_max_call_duration_seconds: int = Field(default=1800, ge=60, le=7200)
    hotline_outbound_ring_timeout_seconds: int = Field(default=30, ge=5, le=600)
    hotline_carrier_admission_ttl_seconds: int = Field(default=300, ge=60, le=900)
    hotline_retry_attempts: int = Field(default=2, ge=0, le=5)

    @field_validator(
        "public_base_url",
        "hotline_daemon_url",
        "hotline_fallback_webhook_url",
        mode="before",
    )
    @classmethod
    def strip_trailing_slash(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value.rstrip("/") or None
        return value

    @field_validator(
        "openai_project_id",
        "twilio_account_sid",
        "twilio_phone_number",
        "vobiz_auth_id",
        "vobiz_phone_number",
        "vapi_assistant_id",
        "vapi_phone_number_id",
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("owner_confirmation_pin")
    @classmethod
    def validate_owner_confirmation_pin(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and (not 6 <= len(raw) <= 12 or not raw.isascii() or not raw.isdigit()):
            raise ValueError("OWNER_CONFIRMATION_PIN must contain 6 to 12 ASCII digits")
        return value

    @field_validator(
        "hotline_local_token",
        "hotline_sip_correlation_secret",
        "hotline_action_signing_secret",
        "hotline_fallback_signing_secret",
        "hotline_fallback_webhook_token",
        "vapi_webhook_token",
    )
    @classmethod
    def validate_hotline_secret_strength(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and len(raw) < 32:
            raise ValueError("Hotline service secrets must contain at least 32 characters")
        return value

    @field_validator("owner_phone_number")
    @classmethod
    def validate_owner_phone_number(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if raw and re.fullmatch(r"\+[1-9]\d{7,14}", raw) is None:
            raise ValueError("OWNER_PHONE_NUMBER must be strict E.164")
        return value

    @field_validator("twilio_phone_number")
    @classmethod
    def validate_twilio_phone_number(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"\+[1-9]\d{7,14}", value) is None:
            raise ValueError("TWILIO_PHONE_NUMBER must be strict E.164")
        return value

    @field_validator("vobiz_phone_number")
    @classmethod
    def validate_vobiz_phone_number(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"\+[1-9]\d{7,14}", value) is None:
            raise ValueError("VOBIZ_PHONE_NUMBER must be strict E.164")
        return value

    @field_validator("vobiz_auth_id")
    @classmethod
    def validate_vobiz_auth_id(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"MA_[A-Za-z0-9]{4,64}", value) is None:
            raise ValueError("VOBIZ_AUTH_ID must be an MA_-prefixed account ID")
        return value

    @field_validator("openai_project_id")
    @classmethod
    def validate_openai_project_id(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.startswith("proj_") or not value.replace("_", "").replace("-", "").isalnum()
        ):
            raise ValueError("OPENAI_PROJECT_ID must be a project ID with a proj_ prefix")
        return value

    @field_validator("openai_realtime_model")
    @classmethod
    def validate_realtime_model(cls, value: str) -> str:
        value = value.strip()
        if value not in {"gpt-realtime-2", "gpt-realtime-2.1"}:
            raise ValueError("OPENAI_REALTIME_MODEL must be gpt-realtime-2 or gpt-realtime-2.1")
        return value

    @field_validator("hotline_owner_name")
    @classmethod
    def normalize_owner_name(cls, value: str) -> str:
        return " ".join(value.split())

    @model_validator(mode="after")
    def hotline_secrets_are_independent(self) -> Settings:
        configured = [
            value
            for value in (
                self.hotline_local_token.get_secret_value(),
                self.hotline_sip_correlation_secret.get_secret_value(),
                self.hotline_action_signing_secret.get_secret_value(),
                self.hotline_fallback_signing_secret.get_secret_value(),
                self.hotline_fallback_webhook_token.get_secret_value(),
                self.vapi_webhook_token.get_secret_value(),
            )
            if value
        ]
        if len(configured) != len(set(configured)):
            raise ValueError("Hotline service secrets must be pairwise distinct")
        if self.hotline_env == "production" and self.hotline_demo_auto_execute_actions:
            raise ValueError(
                "HOTLINE_DEMO_AUTO_EXECUTE_ACTIONS cannot be enabled in production"
            )
        return self

    @property
    def openai_realtime_configured(self) -> bool:
        common_ready = all(
            (
                self.openai_api_key.get_secret_value(),
                self.openai_project_id,
                self.public_base_url,
                self.hotline_sip_correlation_secret.get_secret_value(),
                self.hotline_action_signing_secret.get_secret_value(),
                self.owner_confirmation_pin.get_secret_value(),
            )
        )
        return bool(
            common_ready
            and (
                (
                    self.hotline_carrier == "twilio"
                    and self.twilio_bridge_mode == "media_stream"
                )
                or self.openai_webhook_secret.get_secret_value()
            )
        )

    @property
    def twilio_configured(self) -> bool:
        return all(
            (
                self.twilio_account_sid,
                self.twilio_auth_token.get_secret_value(),
                self.twilio_phone_number,
                self.owner_phone_number.get_secret_value(),
                self.openai_project_id,
                self.public_base_url,
                self.hotline_sip_correlation_secret.get_secret_value(),
            )
        )

    @property
    def vobiz_configured(self) -> bool:
        return all(
            (
                self.vobiz_auth_id,
                self.vobiz_auth_token.get_secret_value(),
                self.vobiz_phone_number,
                self.owner_phone_number.get_secret_value(),
                self.openai_project_id,
                self.openai_webhook_secret.get_secret_value(),
                self.public_base_url,
                self.hotline_sip_correlation_secret.get_secret_value(),
            )
        )

    @property
    def carrier_configured(self) -> bool:
        if self.hotline_carrier == "vobiz":
            return bool(self.vobiz_configured)
        return bool(self.twilio_configured)

    @property
    def vapi_configured(self) -> bool:
        return all(
            (
                self.vapi_webhook_token.get_secret_value(),
                self.vapi_assistant_id,
                self.vapi_phone_number_id,
                self.owner_phone_number.get_secret_value(),
            )
        )

    @property
    def openai_sip_uri(self) -> str | None:
        if self.openai_project_id is None:
            return None
        return f"sip:{self.openai_project_id}@sip.api.openai.com;transport=tls"

    @property
    def openai_realtime_runtime_ready(self) -> bool:
        return bool(
            self.openai_realtime_configured
            and self.carrier_configured
            and self.hotline_local_token.get_secret_value()
        )

    @property
    def secure_fallback_configured(self) -> bool:
        return bool(
            self.public_base_url
            and self.hotline_fallback_webhook_url
            and self.hotline_fallback_webhook_token.get_secret_value()
            and self.hotline_fallback_signing_secret.get_secret_value()
            and self.owner_confirmation_pin.get_secret_value()
        )

    @property
    def allowlisted_callers(self) -> tuple[str, ...]:
        return tuple(
            item.strip() for item in self.hotline_allowlisted_callers.split(",") if item.strip()
        )

    @property
    def workspace_roots(self) -> tuple[Path, ...]:
        return tuple(
            Path(item.strip()).expanduser()
            for item in self.hotline_workspace_roots.replace("\r", "\n")
            .replace("\n", ";")
            .split(";")
            if item.strip()
        )

    def ensure_runtime_directory(self) -> None:
        self.hotline_database_path.parent.mkdir(parents=True, exist_ok=True)

    def diagnostics(self) -> dict[str, Any]:
        """Return presence-only configuration data safe for logs and CLI output."""

        return {
            "environment": self.hotline_env,
            "transport": self.hotline_transport,
            "daemon_url": self.hotline_daemon_url,
            "database_path": str(self.hotline_database_path),
            "public_base_url_configured": bool(self.public_base_url),
            "local_token_configured": bool(self.hotline_local_token.get_secret_value()),
            "sip_correlation_secret_configured": bool(
                self.hotline_sip_correlation_secret.get_secret_value()
            ),
            "action_signing_secret_configured": bool(
                self.hotline_action_signing_secret.get_secret_value()
            ),
            "fallback_signing_secret_configured": bool(
                self.hotline_fallback_signing_secret.get_secret_value()
            ),
            "secure_fallback_configured": self.secure_fallback_configured,
            "fallback_webhook_configured": bool(self.hotline_fallback_webhook_url),
            "fallback_webhook_token_configured": bool(
                self.hotline_fallback_webhook_token.get_secret_value()
            ),
            "openai_api_key_configured": bool(self.openai_api_key.get_secret_value()),
            "openai_webhook_secret_configured": bool(self.openai_webhook_secret.get_secret_value()),
            "openai_project_configured": bool(self.openai_project_id),
            "openai_realtime_model": self.openai_realtime_model,
            "openai_realtime_voice": self.openai_realtime_voice,
            "openai_realtime_reasoning_effort": self.openai_realtime_reasoning_effort,
            "openai_realtime_configured": self.openai_realtime_configured,
            "openai_realtime_runtime_ready": self.openai_realtime_runtime_ready,
            "carrier": self.hotline_carrier,
            "carrier_configured": self.carrier_configured,
            "twilio_account_configured": bool(self.twilio_account_sid),
            "twilio_auth_token_configured": bool(self.twilio_auth_token.get_secret_value()),
            "twilio_number_configured": bool(self.twilio_phone_number),
            "twilio_bridge_mode": self.twilio_bridge_mode,
            "twilio_configured": self.twilio_configured,
            "vobiz_auth_id_configured": bool(self.vobiz_auth_id),
            "vobiz_auth_token_configured": bool(self.vobiz_auth_token.get_secret_value()),
            "vobiz_number_configured": bool(self.vobiz_phone_number),
            "vobiz_configured": self.vobiz_configured,
            "vapi_webhook_token_configured": bool(
                self.vapi_webhook_token.get_secret_value()
            ),
            "vapi_assistant_id_configured": bool(self.vapi_assistant_id),
            "vapi_phone_number_id_configured": bool(self.vapi_phone_number_id),
            "vapi_configured": self.vapi_configured,
            "owner_number_configured": bool(self.owner_phone_number.get_secret_value()),
            "owner_confirmation_pin_configured": bool(
                self.owner_confirmation_pin.get_secret_value()
            ),
            "codex_app_server_enabled": self.codex_app_server_enabled,
            "workspace_roots_configured": len(self.workspace_roots),
            "git_bin_configured": self.hotline_git_bin is not None,
            "codex_writes_enabled": self.hotline_allow_codex_writes,
            "demo_auto_execute_actions": self.hotline_demo_auto_execute_actions,
            "real_runbooks_enabled": self.hotline_allow_real_runbooks,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings for tests or an explicit runtime reload."""

    get_settings.cache_clear()
