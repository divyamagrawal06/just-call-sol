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
from agent_hotline.settings import Settings, reset_settings_cache

runner = CliRunner()


def test_serve_disables_access_logs_that_would_expose_callback_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        hotline_database_path=tmp_path / "hotline.sqlite3",
        hotline_callback_token="callback-secret-that-must-not-log",
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
    assert settings.hotline_callback_token.get_secret_value() not in repr(captured)


def test_cli_help_exposes_portable_integration_commands() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "install-clients" in result.output
    assert "serve" in result.output
    assert "doctor" in result.output


def test_doctor_never_prints_configured_secret_values(monkeypatch) -> None:
    sentinel = "this-secret-must-never-appear"
    monkeypatch.setenv("SARVAM_API_KEY", sentinel)
    monkeypatch.setenv("HOTLINE_LOCAL_TOKEN", sentinel)
    monkeypatch.setenv("HOTLINE_TOOL_TOKEN", sentinel)
    monkeypatch.setenv("HOTLINE_CALLBACK_TOKEN", sentinel)
    reset_settings_cache()
    try:
        result = runner.invoke(cli.app, ["doctor"])
    finally:
        reset_settings_cache()

    assert result.exit_code == 0
    assert sentinel not in result.output
    assert "sarvam_api_key_configured" in result.output


def test_install_clients_constructs_codex_and_claude_commands(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    marketplace_path = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(
        json.dumps({"name": "test-marketplace"}),
        encoding="utf-8",
    )
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
    monkeypatch.setattr(cli, "_claude_registration_needed", lambda: True)

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
    assert "marketplace manifest is missing" in result.output
    assert commands == []


def test_install_clients_is_a_noop_when_integrations_are_registered(
    monkeypatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    marketplace_path = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace_path.parent.mkdir(parents=True)
    marketplace_path.write_text(json.dumps({"name": "test-marketplace"}), encoding="utf-8")
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
    monkeypatch.setattr(cli, "_claude_registration_needed", lambda: False)

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


def test_codex_registration_plan_skips_matching_marketplace_and_plugin(
    monkeypatch,
    tmp_path: Path,
) -> None:
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


def test_codex_registration_plan_refuses_marketplace_name_collision(
    monkeypatch,
    tmp_path: Path,
) -> None:
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

    assert cli._claude_registration_needed() is False


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
