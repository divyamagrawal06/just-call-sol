from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from agent_hotline import cli
from agent_hotline.contracts import ContactHumanResult
from agent_hotline.settings import Settings, reset_settings_cache

runner = CliRunner()


def _write_marketplace_bundle(root: Path, *, name: str = "test-marketplace") -> None:
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True, exist_ok=True)
    marketplace_path.write_text(
        json.dumps(
            {
                "name": name,
                "plugins": [
                    {
                        "name": "agent-hotline",
                        "source": {
                            "source": "local",
                            "path": "./plugins/agent-hotline",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plugin_manifest = root / "plugins" / "agent-hotline" / ".codex-plugin" / "plugin.json"
    plugin_manifest.parent.mkdir(parents=True, exist_ok=True)
    plugin_manifest.write_text(
        json.dumps({"name": "agent-hotline", "version": "0.2.0"}),
        encoding="utf-8",
    )


def test_serve_disables_access_logs_that_would_expose_callback_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_sip_correlation_secret=("correlation-secret-that-must-not-log-123456"),
        codex_app_server_enabled=False,
    )
    captured: dict[str, Any] = {}

    def fake_run(*args: object, **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(uvicorn, "run", fake_run)

    cli.serve()

    assert captured["kwargs"]["access_log"] is False
    assert settings.hotline_sip_correlation_secret.get_secret_value() not in repr(captured)


def test_cli_help_exposes_portable_integration_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "install-clients" in result.output
    assert "serve" in result.output
    assert "doctor" in result.output


def test_call_no_wait_starts_a_blocking_decision_event_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_contact(request, *, start_only: bool = False):
        captured["request"] = request
        captured["start_only"] = start_only
        return {"event_id": "evt_cli_no_wait", "status": "calling"}

    monkeypatch.setattr(cli, "_contact", fake_contact)

    result = runner.invoke(
        cli.app,
        [
            "call",
            "--summary",
            "The deployment is blocked.",
            "--question",
            "Approve the bounded retry?",
            "--timeout",
            "90",
            "--no-wait",
        ],
    )

    assert result.exit_code == 0
    request = captured["request"]
    assert request.wait_for_decision is True
    assert request.timeout_seconds == 90
    assert captured["start_only"] is True
    assert "evt_cli_no_wait" in result.stdout


@pytest.mark.asyncio
async def test_event_result_watch_polls_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = iter(("calling", "resolved"))

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def get_result(self, event_id: str) -> ContactHumanResult:
            return ContactHumanResult(event_id=event_id, status=next(statuses))

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(cli, "HotlineClient", FakeClient)
    monkeypatch.setattr(cli.asyncio, "sleep", no_sleep)

    payload, terminal = await cli._event_result(
        "evt_cli_watch",
        watch=True,
        timeout_seconds=10,
        interval_seconds=1,
    )

    assert terminal is True
    assert payload["status"] == "resolved"


def test_doctor_never_prints_configured_secret_values(monkeypatch) -> None:
    secrets = {
        "OPENAI_API_KEY": "openai-secret-must-never-appear",
        "OPENAI_WEBHOOK_SECRET": "webhook-secret-must-never-appear",
        "TWILIO_AUTH_TOKEN": "twilio-secret-must-never-appear",
        "HOTLINE_LOCAL_TOKEN": "local-secret-must-never-appear-123456",
        "HOTLINE_SIP_CORRELATION_SECRET": ("correlation-secret-must-never-appear-123456"),
        "HOTLINE_ACTION_SIGNING_SECRET": ("action-secret-must-never-appear-123456"),
        "HOTLINE_FALLBACK_SIGNING_SECRET": ("fallback-secret-must-never-appear-123456"),
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    reset_settings_cache()
    try:
        result = runner.invoke(cli.app, ["doctor"])
    finally:
        reset_settings_cache()

    assert result.exit_code == 0
    assert all(value not in result.output for value in secrets.values())
    assert "openai_api_key_configured" in result.output
    assert "openai_webhook_secret_configured" in result.output
    assert "twilio_auth_token_configured" in result.output


def test_posix_init_secrets_merges_unknown_settings_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_file = tmp_path / "config" / "agent-hotline" / "runtime.env"
    runtime_file.parent.mkdir(parents=True)
    runtime_file.write_text(
        "# User-owned provider settings must remain byte-for-byte.\n"
        "export OPENAI_API_KEY='keep-openai-key'\n"
        "UNKNOWN_SETTING=keep-this-value\n"
        "\n"
        "export HOTLINE_LOCAL_TOKEN='existing-local-token'\n"
        "HOTLINE_ACTION_SIGNING_SECRET=existing-action-token\n",
        encoding="utf-8",
    )
    for name in cli._MANAGED_SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli, "runtime_env_path", lambda: runtime_file)
    generated_values = iter(
        (
            "generated-sip-correlation-token",
            "generated-fallback-signing-token",
            "generated-fallback-webhook-token",
        )
    )
    generation_calls: list[int] = []

    def generate_secret(length: int) -> str:
        generation_calls.append(length)
        return next(generated_values)

    monkeypatch.setattr(cli.secrets, "token_urlsafe", generate_secret)

    first = runner.invoke(cli.app, ["init-secrets"])
    first_content = runtime_file.read_text(encoding="utf-8")
    second = runner.invoke(cli.app, ["init-secrets"])
    second_content = runtime_file.read_text(encoding="utf-8")

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first_content == second_content
    assert generation_calls == [32, 32, 32]
    assert "# User-owned provider settings must remain byte-for-byte.\n" in first_content
    assert "export OPENAI_API_KEY='keep-openai-key'\n" in first_content
    assert "UNKNOWN_SETTING=keep-this-value\n" in first_content
    assert "HOTLINE_LOCAL_TOKEN=existing-local-token\n" in first_content
    assert "HOTLINE_ACTION_SIGNING_SECRET=existing-action-token\n" in first_content
    assert "HOTLINE_SIP_CORRELATION_SECRET=generated-sip-correlation-token\n" in first_content
    assert "HOTLINE_FALLBACK_SIGNING_SECRET=generated-fallback-signing-token\n" in first_content
    assert "HOTLINE_FALLBACK_WEBHOOK_TOKEN=generated-fallback-webhook-token\n" in first_content
    for name in cli._MANAGED_SECRET_NAMES:
        assert first_content.count(f"{name}=") == 1
    if cli.os.name != "nt":
        assert runtime_file.stat().st_mode & 0o777 == 0o600
    for secret in (
        "existing-local-token",
        "existing-action-token",
        "generated-sip-correlation-token",
        "generated-fallback-signing-token",
        "generated-fallback-webhook-token",
    ):
        assert secret not in first.output
        assert secret not in second.output


def test_posix_init_secrets_force_rotates_only_managed_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime_file = tmp_path / "config" / "agent-hotline" / "runtime.env"
    runtime_file.parent.mkdir(parents=True)
    original_managed = {
        name: f"old-{index}-managed-token"
        for index, name in enumerate(cli._MANAGED_SECRET_NAMES, start=1)
    }
    runtime_file.write_text(
        "# Preserve this comment.\n"
        "OPENAI_API_KEY=keep-provider-setting\n"
        "CUSTOM_AGENT_SETTING='keep custom spacing'\n"
        + "".join(f"{name}={value}\n" for name, value in original_managed.items()),
        encoding="utf-8",
    )
    for name in cli._MANAGED_SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli, "runtime_env_path", lambda: runtime_file)
    rotated_values = [
        f"rotated-{index}-managed-token" for index in range(1, len(cli._MANAGED_SECRET_NAMES) + 1)
    ]
    generated_values = iter(rotated_values)
    generation_calls: list[int] = []

    def generate_secret(length: int) -> str:
        generation_calls.append(length)
        return next(generated_values)

    monkeypatch.setattr(cli.secrets, "token_urlsafe", generate_secret)

    result = runner.invoke(cli.app, ["init-secrets", "--force"])
    content = runtime_file.read_text(encoding="utf-8")

    assert result.exit_code == 0
    assert generation_calls == [32] * len(cli._MANAGED_SECRET_NAMES)
    assert "# Preserve this comment.\n" in content
    assert "OPENAI_API_KEY=keep-provider-setting\n" in content
    assert "CUSTOM_AGENT_SETTING='keep custom spacing'\n" in content
    for name, rotated in zip(cli._MANAGED_SECRET_NAMES, rotated_values, strict=True):
        assert f"{name}={rotated}\n" in content
        assert content.count(f"{name}=") == 1
        assert rotated not in result.output
    for original in original_managed.values():
        assert original not in content
        assert original not in result.output
    if cli.os.name != "nt":
        assert runtime_file.stat().st_mode & 0o777 == 0o600


def test_install_clients_constructs_codex_and_claude_commands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    _write_marketplace_bundle(tmp_path)
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_checked_run", lambda command: commands.append(command))
    monkeypatch.setattr(cli, "_uv_tool_install_needed", lambda _root: True)
    monkeypatch.setattr(
        cli,
        "_plan_codex_registration",
        lambda _root, _name: cli._CodexRegistrationPlan(
            add_marketplace=True,
            add_plugin=True,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_plan_claude_registration",
        lambda: cli._ClaudeRegistrationPlan(
            remove_existing=False,
            add_server=True,
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "install-clients",
            "--client",
            "all",
            "--marketplace-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert commands[0] == [
        "uv",
        "tool",
        "install",
        "--editable",
        str(tmp_path.resolve()),
    ]
    assert commands[1] == ["codex", "plugin", "marketplace", "add", str(tmp_path.resolve())]
    assert commands[2] == [
        "codex",
        "plugin",
        "add",
        "agent-hotline@test-marketplace",
    ]
    assert commands[3] == [
        "claude",
        "mcp",
        "add",
        "--scope",
        "user",
        "agent-hotline",
        "--env",
        "HOTLINE_MCP_CLIENT=claude",
        "--",
        "agent-hotline-mcp",
    ]


def test_install_clients_fails_before_mutation_when_marketplace_is_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_checked_run", lambda command: commands.append(command))

    result = runner.invoke(
        cli.app,
        ["install-clients", "--client", "codex", "--marketplace-root", str(tmp_path)],
    )

    assert result.exit_code != 0
    assert "plugin bundle is incomplete" in result.output
    assert commands == []


def test_install_clients_is_a_noop_when_integrations_are_registered(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    _write_marketplace_bundle(tmp_path)
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_checked_run", lambda command: commands.append(command))
    monkeypatch.setattr(cli, "_uv_tool_install_needed", lambda _root: False)
    monkeypatch.setattr(
        cli,
        "_plan_codex_registration",
        lambda _root, _name: cli._CodexRegistrationPlan(
            add_marketplace=False,
            add_plugin=False,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_plan_claude_registration",
        lambda: cli._ClaudeRegistrationPlan(
            remove_existing=False,
            add_server=False,
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "install-clients",
            "--client",
            "all",
            "--marketplace-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert commands == []
    assert "already installed" in result.output
    assert "already registered" in result.output


def test_uv_tool_install_skips_the_matching_environment_it_is_running_from(
    monkeypatch,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "uv-tools" / "agent-hotline"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text(
        "[tool]\n"
        f'requirements = [{{ name = "agent-hotline", editable = "{tmp_path.as_posix()}" }}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.sys, "prefix", str(environment))

    def unexpected_run(_command: list[str]) -> subprocess.CompletedProcess[str]:
        raise AssertionError("uv tool dir must not be queried from the active matching tool")

    monkeypatch.setattr(cli, "_captured_run", unexpected_run)

    assert cli._uv_tool_install_needed(tmp_path) is False


def test_uv_tool_install_refuses_to_replace_the_environment_it_is_running_from(
    monkeypatch,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "uv-tools" / "agent-hotline"
    other_source = tmp_path / "other-source"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text(
        "[tool]\n"
        f'requirements = [{{ name = "agent-hotline", editable = "{other_source.as_posix()}" }}]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.sys, "prefix", str(environment))

    with pytest.raises(cli.typer.BadParameter, match="active environment"):
        cli._uv_tool_install_needed(tmp_path)


def test_release_install_skips_persistent_uv_tool_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    environment = tmp_path / "uv-tools" / "agent-hotline"
    environment.mkdir(parents=True)
    (environment / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "agent-hotline", version = "0.2.0" }]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.sys, "prefix", str(environment))
    monkeypatch.setattr(
        cli,
        "_captured_run",
        lambda _command: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=str(environment.parent),
            stderr="",
        ),
    )

    assert cli._uv_tool_install_needed(None) is False


def test_release_install_requires_mcp_companion_on_path(
    monkeypatch,
) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    with pytest.raises(cli.typer.BadParameter, match="agent-hotline-mcp is not on PATH"):
        cli._uv_tool_install_needed(None)


def test_marketplace_root_prefers_wheel_bundle_over_checkout_in_cwd(
    monkeypatch,
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "site-packages" / "agent_hotline"
    bundled_root = package_root / "_distribution"
    package_root.mkdir(parents=True)
    _write_marketplace_bundle(bundled_root, name="agent-hotline-local")
    checkout = tmp_path / "checkout"
    _write_marketplace_bundle(checkout, name="checkout-marketplace")
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(cli, "__file__", str(package_root / "cli.py"))

    assert cli._resolve_marketplace_root(None) == bundled_root


def test_marketplace_root_rejects_manifest_without_plugin(
    tmp_path: Path,
) -> None:
    marketplace_path = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(
        json.dumps({"name": "broken", "plugins": []}),
        encoding="utf-8",
    )

    with pytest.raises(cli.typer.BadParameter, match="bundle is incomplete"):
        cli._resolve_marketplace_root(tmp_path)


def test_codex_registration_plan_skips_matching_marketplace_and_plugin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_marketplace_bundle(tmp_path, name="personal")
    responses = iter(
        [
            {
                "marketplaces": [
                    {
                        "name": "personal",
                        "root": str(tmp_path),
                    }
                ]
            },
            {
                "installed": [
                    {
                        "pluginId": "agent-hotline@personal",
                        "version": "0.2.0",
                        "source": {
                            "source": "local",
                            "path": str(tmp_path / "plugins" / "agent-hotline"),
                        },
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        cli,
        "_command_json",
        lambda _command, _description: next(responses),
    )

    plan = cli._plan_codex_registration(tmp_path, "personal")

    assert plan == cli._CodexRegistrationPlan(
        add_marketplace=False,
        add_plugin=False,
    )


def test_codex_registration_plan_reinstalls_stale_plugin_snapshot(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_marketplace_bundle(tmp_path, name="agent-hotline-local")
    responses = iter(
        [
            {
                "marketplaces": [
                    {
                        "name": "agent-hotline-local",
                        "root": str(tmp_path),
                    }
                ]
            },
            {
                "installed": [
                    {
                        "pluginId": "agent-hotline@agent-hotline-local",
                        "version": "0.1.0",
                        "source": {
                            "source": "local",
                            "path": str(tmp_path / "plugins" / "agent-hotline"),
                        },
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        cli,
        "_command_json",
        lambda _command, _description: next(responses),
    )

    assert cli._plan_codex_registration(
        tmp_path,
        "agent-hotline-local",
    ) == cli._CodexRegistrationPlan(
        add_marketplace=False,
        add_plugin=True,
    )


def test_codex_registration_plan_requires_explicit_legacy_plugin_migration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_marketplace_bundle(tmp_path, name="agent-hotline-local")
    responses = iter(
        [
            {
                "marketplaces": [
                    {
                        "name": "agent-hotline-local",
                        "root": str(tmp_path),
                    }
                ]
            },
            {
                "installed": [
                    {
                        "pluginId": "agent-hotline@personal",
                        "name": "agent-hotline",
                        "marketplaceName": "personal",
                        "version": "0.1.0",
                        "source": {
                            "source": "local",
                            "path": str(tmp_path / "plugins" / "agent-hotline"),
                        },
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        cli,
        "_command_json",
        lambda _command, _description: next(responses),
    )

    with pytest.raises(
        cli.typer.BadParameter,
        match=r"codex plugin remove agent-hotline@personal",
    ):
        cli._plan_codex_registration(tmp_path, "agent-hotline-local")


def test_codex_registration_plan_refuses_marketplace_name_collision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _write_marketplace_bundle(tmp_path)
    monkeypatch.setattr(
        cli,
        "_command_json",
        lambda _command, _description: {
            "marketplaces": [{"name": "personal", "root": str(tmp_path / "other")}]
        },
    )

    with pytest.raises(cli.typer.BadParameter, match="different source"):
        cli._plan_codex_registration(tmp_path, "personal")


def test_claude_registration_check_skips_matching_user_stdio_server(
    monkeypatch,
) -> None:
    output = (
        "agent-hotline:\n"
        "  Scope: User config (available in all your projects)\n"
        "  Status: Connected\n"
        "  Type: stdio\n"
        "  Command: agent-hotline-mcp\n"
        "  Args:\n"
        "  Environment: HOTLINE_MCP_CLIENT=claude\n"
    )
    monkeypatch.setattr(
        cli,
        "_captured_run",
        lambda _command: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=output,
            stderr="",
        ),
    )

    assert cli._claude_registration_needed() is False


def test_claude_registration_plan_repairs_missing_client_identity(
    monkeypatch,
) -> None:
    output = (
        "agent-hotline:\n"
        "  Scope: User config (available in all your projects)\n"
        "  Status: Connected\n"
        "  Type: stdio\n"
        "  Command: agent-hotline-mcp\n"
        "  Args:\n"
        "  Environment:\n"
    )
    monkeypatch.setattr(
        cli,
        "_captured_run",
        lambda _command: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=output,
            stderr="",
        ),
    )

    assert cli._plan_claude_registration() == cli._ClaudeRegistrationPlan(
        remove_existing=True,
        add_server=True,
    )


def test_install_clients_repairs_matching_legacy_claude_registration(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    _write_marketplace_bundle(tmp_path)
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_checked_run", lambda command: commands.append(command))
    monkeypatch.setattr(cli, "_uv_tool_install_needed", lambda _root: False)
    monkeypatch.setattr(
        cli,
        "_plan_claude_registration",
        lambda: cli._ClaudeRegistrationPlan(
            remove_existing=True,
            add_server=True,
        ),
    )

    result = runner.invoke(
        cli.app,
        [
            "install-clients",
            "--client",
            "claude",
            "--marketplace-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert commands == [
        [
            "claude",
            "mcp",
            "remove",
            "--scope",
            "user",
            "agent-hotline",
        ],
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "agent-hotline",
            "--env",
            "HOTLINE_MCP_CLIENT=claude",
            "--",
            "agent-hotline-mcp",
        ],
    ]


def test_checked_run_prefers_native_executable_on_windows(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_which(name: str) -> str | None:
        if name == "codex":
            return r"C:\tools\codex.exe"
        return None

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.shutil, "which", fake_which)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli._checked_run(["codex", "plugin", "list"])

    assert calls == [
        (
            [r"C:\tools\codex.exe", "plugin", "list"],
            {"check": False, "shell": False},
        )
    ]


def test_checked_run_invokes_cmd_shim_via_comspec_on_windows(monkeypatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    shim = r"C:\Users\Test User\AppData\Roaming\npm\claude.CMD"
    comspec = r"C:\Windows\System32\cmd.exe"

    def fake_which(name: str) -> str | None:
        return {"claude.exe": None, "claude": shim}.get(name)

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.shutil, "which", fake_which)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setenv("COMSPEC", comspec)
    original = ["claude", "mcp", "add", "--scope", "user", "agent-hotline"]

    cli._checked_run(original)

    expected_line = subprocess.list2cmdline([shim, *original[1:]])
    assert calls == [
        (
            [comspec, "/d", "/c", expected_line],
            {"check": False, "shell": False},
        )
    ]


def test_captured_run_reports_missing_executable(monkeypatch) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _name: None)

    def missing_run(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("missing")

    monkeypatch.setattr(cli.subprocess, "run", missing_run)

    with pytest.raises(cli.typer.BadParameter, match="unavailable: claude"):
        cli._captured_run(["claude", "mcp", "get", "agent-hotline"])
