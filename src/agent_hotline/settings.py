"""Runtime configuration with secret-safe diagnostics."""

from __future__ import annotations

import os
import platform
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_WINDOWS_USER_ENV_KEYS = (
    "SARVAM_API_KEY",
    "SARVAM_ORG_ID",
    "SARVAM_WORKSPACE_ID",
    "SARVAM_APP_ID",
    "SARVAM_APP_VERSION",
    "SARVAM_CONNECTION_ID",
    "SARVAM_AGENT_PHONE_NUMBER",
    "SARVAM_INBOUND_SCHEDULE",
    "OWNER_PHONE_NUMBER",
    "OWNER_CONFIRMATION_PIN",
    "HOTLINE_TOOL_TOKEN",
    "HOTLINE_PUBLIC_TOOLS_REQUIRE_TOKEN",
    "HOTLINE_LOCAL_TOKEN",
    "HOTLINE_CALLBACK_TOKEN",
    "HOTLINE_FALLBACK_WEBHOOK_URL",
    "HOTLINE_FALLBACK_WEBHOOK_TOKEN",
    "HOTLINE_FALLBACK_TTL_SECONDS",
    "HOTLINE_FALLBACK_MAX_PIN_ATTEMPTS",
    "HOTLINE_GIT_BIN",
    "HOTLINE_WORKSPACE_ROOTS",
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


class Settings(BaseSettings):
    """Configuration for the daemon, MCP bridge, and provider client."""

    model_config = SettingsConfigDict(
        env_file=(".env", ".hotline/runtime.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    hotline_env: Literal["development", "test", "production"] = "development"
    hotline_host: str = "127.0.0.1"
    hotline_port: int = Field(default=8787, ge=1, le=65535)
    hotline_database_path: Path = Path(".hotline/hotline.db")
    hotline_daemon_url: str = "http://127.0.0.1:8787"
    hotline_tool_token: SecretStr = SecretStr("")
    # Sarvam rejects some custom Authorization configurations before making the
    # request. Development demos may explicitly disable bearer auth for the
    # Sarvam tool surface; coordinator session/PIN/readback/grant gates remain.
    hotline_public_tools_require_token: bool = True
    hotline_local_token: SecretStr = SecretStr("")
    hotline_callback_token: SecretStr = SecretStr("")
    hotline_fallback_webhook_url: str | None = None
    hotline_fallback_webhook_token: SecretStr = SecretStr("")
    hotline_fallback_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    hotline_fallback_max_pin_attempts: int = Field(default=5, ge=1, le=10)
    hotline_decision_timeout_seconds: int = Field(default=600, ge=1, le=1200)
    hotline_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    public_base_url: str | None = None

    sarvam_api_key: SecretStr = SecretStr("")
    sarvam_org_id: str | None = None
    sarvam_workspace_id: str | None = None
    sarvam_app_id: str | None = None
    sarvam_app_version: int = Field(default=1, ge=1)
    sarvam_connection_id: str | None = None
    sarvam_agent_phone_number: str | None = None
    # Optional atomic JSON object. When absent, Sarvam's documented 24/7 default
    # is preserved by omitting inbound_config from the deployment request.
    sarvam_inbound_schedule: str | None = None
    owner_phone_number: SecretStr = SecretStr("")
    owner_confirmation_pin: SecretStr = SecretStr("")

    codex_bin: str = "codex"
    codex_app_server_enabled: bool = True
    codex_app_server_cwd: Path | None = None
    # Semicolon-separated repository roots. An empty value safely falls back to
    # the daemon's single configured Codex cwd, not to the user's home directory.
    hotline_workspace_roots: str = ""
    hotline_git_bin: Path | None = None

    hotline_transport: Literal["sarvam", "fake", "disabled"] = "sarvam"
    hotline_allow_real_actions: bool = False
    hotline_allowlisted_callers: str = ""
    hotline_max_active_calls: int = Field(default=1, ge=1, le=10)
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
        "sarvam_org_id",
        "sarvam_workspace_id",
        "sarvam_app_id",
        "sarvam_connection_id",
        "sarvam_agent_phone_number",
        "sarvam_inbound_schedule",
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

    @model_validator(mode="after")
    def reject_headerless_public_tools_in_production(self) -> Settings:
        if self.hotline_env == "production" and not self.hotline_public_tools_require_token:
            raise ValueError("HOTLINE_PUBLIC_TOOLS_REQUIRE_TOKEN cannot be disabled in production")
        return self

    @property
    def sarvam_configured(self) -> bool:
        return all(
            (
                self.sarvam_api_key.get_secret_value(),
                self.sarvam_org_id,
                self.sarvam_workspace_id,
                self.sarvam_app_id,
                self.sarvam_connection_id,
                self.sarvam_agent_phone_number,
                self.owner_phone_number.get_secret_value(),
            )
        )

    @property
    def public_tools_configured(self) -> bool:
        return bool(
            self.public_base_url
            and self.hotline_callback_token.get_secret_value()
            and (
                not self.hotline_public_tools_require_token
                or self.hotline_tool_token.get_secret_value()
            )
        )

    @property
    def secure_fallback_configured(self) -> bool:
        return bool(
            self.public_base_url
            and self.hotline_fallback_webhook_url
            and self.hotline_fallback_webhook_token.get_secret_value()
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
            "tool_token_configured": bool(self.hotline_tool_token.get_secret_value()),
            "public_tools_require_token": self.hotline_public_tools_require_token,
            "local_token_configured": bool(self.hotline_local_token.get_secret_value()),
            "callback_token_configured": bool(self.hotline_callback_token.get_secret_value()),
            "secure_fallback_configured": self.secure_fallback_configured,
            "fallback_webhook_configured": bool(self.hotline_fallback_webhook_url),
            "fallback_webhook_token_configured": bool(
                self.hotline_fallback_webhook_token.get_secret_value()
            ),
            "sarvam_api_key_configured": bool(self.sarvam_api_key.get_secret_value()),
            "sarvam_org_configured": bool(self.sarvam_org_id),
            "sarvam_workspace_configured": bool(self.sarvam_workspace_id),
            "sarvam_app_configured": bool(self.sarvam_app_id),
            "sarvam_connection_configured": bool(self.sarvam_connection_id),
            "sarvam_number_configured": bool(self.sarvam_agent_phone_number),
            "sarvam_inbound_schedule_configured": bool(self.sarvam_inbound_schedule),
            "owner_number_configured": bool(self.owner_phone_number.get_secret_value()),
            "owner_confirmation_pin_configured": bool(
                self.owner_confirmation_pin.get_secret_value()
            ),
            "codex_app_server_enabled": self.codex_app_server_enabled,
            "workspace_roots_configured": len(self.workspace_roots),
            "git_bin_configured": self.hotline_git_bin is not None,
            "real_actions_enabled": self.hotline_allow_real_actions,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Clear cached settings for tests or an explicit runtime reload."""

    get_settings.cache_clear()
