"""Agent Hotline command-line interface."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .client import HotlineClient, HotlineClientError
from .contracts import ContactHumanRequest, ContextPacket
from .settings import get_settings, reset_settings_cache

app = typer.Typer(
    name="agent-hotline",
    help="Voice control plane for Codex and Claude agents.",
    no_args_is_help=True,
)
console = Console()

_TOOL_DISTRIBUTION = "agent-hotline"
_MCP_EXECUTABLE = "agent-hotline-mcp"


@dataclass(frozen=True)
class _CodexRegistrationPlan:
    add_marketplace: bool
    add_plugin: bool


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option(help="Bind host override.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port override.")] = None,
    reload: Annotated[bool, typer.Option(help="Enable development reload.")] = False,
) -> None:
    """Run the persistent Hotline daemon."""

    import uvicorn

    settings = get_settings()
    settings.ensure_runtime_directory()
    uvicorn.run(
        "agent_hotline.api:create_app",
        factory=True,
        host=host or settings.hotline_host,
        port=port or settings.hotline_port,
        reload=reload,
        log_level=settings.hotline_log_level.lower(),
        # The webhook authentication secret is part of its callback path because
        # Sarvam does not publish a signature header. Never emit request URLs.
        access_log=False,
    )


@app.command("doctor")
def doctor(
    live: Annotated[
        bool,
        typer.Option(help="Also query the local daemon and Sarvam deployment API."),
    ] = False,
) -> None:
    """Check configuration without printing any credential values."""

    settings = get_settings()
    table = Table(title=f"Agent Hotline {__version__}")
    table.add_column("Check")
    table.add_column("Result")
    for key, value in settings.diagnostics().items():
        table.add_row(key, _display_value(value))
    for executable in ("python", "uv", settings.codex_bin, "claude", "cloudflared"):
        table.add_row(f"executable:{executable}", shutil.which(executable) or "missing")
    console.print(table)

    if live:
        raise typer.Exit(code=asyncio.run(_doctor_live(settings)))


async def _doctor_live(settings: object) -> int:
    exit_code = 0
    try:
        async with HotlineClient() as client:
            health = await client.health()
        console.print("[green]Local daemon:[/green] healthy")
        console.print_json(data=health.model_dump(mode="json"))
    except HotlineClientError as exc:
        console.print(f"[yellow]Local daemon:[/yellow] {exc}")
        exit_code = 1

    from .sarvam import SarvamAPIError, SarvamClient

    typed_settings = get_settings()
    if typed_settings.sarvam_api_key.get_secret_value():
        try:
            async with SarvamClient(typed_settings) as client:
                deployments = await client.list_deployments(limit=10)
            console.print(
                f"[green]Sarvam API:[/green] reachable; {deployments.total} deployment(s)"
            )
        except SarvamAPIError as exc:
            console.print(
                f"[yellow]Sarvam API:[/yellow] {exc} (status={exc.status_code or 'network'})"
            )
            exit_code = 1
    else:
        console.print("[yellow]Sarvam API:[/yellow] key not configured")
        exit_code = 1
    return exit_code


@app.command("init-secrets")
def init_secrets(
    force: Annotated[
        bool,
        typer.Option(help="Rotate existing Hotline-only tokens."),
    ] = False,
) -> None:
    """Generate local service tokens without displaying them."""

    names = (
        "HOTLINE_TOOL_TOKEN",
        "HOTLINE_LOCAL_TOKEN",
        "HOTLINE_CALLBACK_TOKEN",
        "HOTLINE_FALLBACK_WEBHOOK_TOKEN",
    )
    generated: dict[str, str] = {}
    for name in names:
        existing = os.environ.get(name) or _read_windows_user_env(name)
        generated[name] = secrets.token_urlsafe(32) if force or not existing else existing

    if platform.system() == "Windows":
        for name, value in generated.items():
            result = subprocess.run(
                ["setx.exe", name, value],
                check=False,
                capture_output=True,
                text=True,
                shell=False,
            )
            if result.returncode != 0:
                raise typer.BadParameter(f"Could not store {name} in the user environment")
            os.environ[name] = value
        destination = "Windows user environment"
    else:
        runtime_file = Path(".hotline/runtime.env")
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text(
            "".join(f"{name}={value}\n" for name, value in generated.items()),
            encoding="utf-8",
        )
        runtime_file.chmod(0o600)
        destination = str(runtime_file)

    reset_settings_cache()
    console.print(f"[green]Stored four Hotline service tokens in {destination}.[/green]")
    console.print("No token values were printed.")


@app.command("call")
def call(
    summary: Annotated[str, typer.Option(prompt=True, help="One-sentence factual summary.")],
    question: Annotated[str, typer.Option(prompt=True, help="The exact decision needed.")],
    kind: Annotated[
        Literal[
            "approval",
            "clarification",
            "incident",
            "compute_interrupted",
            "authentication",
            "completion",
            "provider_failure",
            "other",
        ],
        typer.Option(),
    ] = "clarification",
    severity: Annotated[
        Literal["info", "low", "medium", "high", "critical"], typer.Option()
    ] = "medium",
    timeout: Annotated[int, typer.Option(min=1, max=1200)] = 600,
    no_wait: Annotated[bool, typer.Option(help="Return once the call is queued.")] = False,
) -> None:
    """Place one real or configured-provider escalation call."""

    request = ContactHumanRequest(
        source="manual",
        kind=kind,
        severity=severity,
        summary=summary,
        question=question,
        context=ContextPacket(task_summary=summary),
        wait_for_decision=not no_wait,
        timeout_seconds=timeout if not no_wait else 1,
    )
    result = asyncio.run(_contact(request))
    console.print_json(data=result)


async def _contact(request: ContactHumanRequest) -> dict[str, object]:
    async with HotlineClient() as client:
        result = await client.contact_human(request)
    return result.model_dump(mode="json", exclude_none=True)


@app.command("events")
def events(
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List recent escalation events."""

    rows = asyncio.run(_events(limit))
    if as_json:
        console.print_json(data=[row.model_dump(mode="json") for row in rows])
        return
    table = Table(title="Recent Agent Hotline events")
    for heading in ("Event", "Kind", "Severity", "State", "Summary"):
        table.add_column(heading)
    for row in rows:
        table.add_row(row.event_id, row.kind, row.severity, row.state, row.summary)
    console.print(table)


async def _events(limit: int):
    async with HotlineClient() as client:
        return await client.list_events(limit)


@app.command("install-clients")
def install_clients(
    client: Annotated[
        Literal["codex", "claude", "all"],
        typer.Option(help="Client configuration to install."),
    ] = "all",
    marketplace_root: Annotated[
        Path,
        typer.Option(help="Repository root containing .agents/plugins/marketplace.json."),
    ] = Path.cwd(),
) -> None:
    """Install the editable CLI and register Codex/Claude MCP integrations."""

    project_root = marketplace_root.resolve()
    if not (project_root / "pyproject.toml").is_file():
        raise typer.BadParameter(
            f"{project_root} is not an Agent Hotline repository (pyproject.toml is missing)"
        )

    marketplace_name: str | None = None
    if client in {"codex", "all"}:
        marketplace_name = _read_marketplace_name(project_root)

    install_tool = _uv_tool_install_needed(project_root)
    codex_plan: _CodexRegistrationPlan | None = None
    if client in {"codex", "all"}:
        assert marketplace_name is not None
        codex_plan = _plan_codex_registration(project_root, marketplace_name)
    install_claude = _claude_registration_needed() if client in {"claude", "all"} else False

    if install_tool:
        _checked_run(["uv", "tool", "install", "--editable", str(project_root)])
    else:
        console.print(
            "[green]Agent Hotline uv tool is already installed from this repository.[/green]"
        )

    if client in {"codex", "all"}:
        assert marketplace_name is not None
        assert codex_plan is not None
        if codex_plan.add_marketplace:
            _checked_run(["codex", "plugin", "marketplace", "add", str(project_root)])
        if codex_plan.add_plugin:
            _checked_run(["codex", "plugin", "add", f"agent-hotline@{marketplace_name}"])
        if codex_plan.add_marketplace or codex_plan.add_plugin:
            console.print("[green]Codex plugin installed.[/green] Start a new task to load it.")
        else:
            console.print("[green]Codex plugin is already registered.[/green]")

    if client in {"claude", "all"}:
        if install_claude:
            _checked_run(
                [
                    "claude",
                    "mcp",
                    "add",
                    "--scope",
                    "user",
                    "agent-hotline",
                    "--",
                    _MCP_EXECUTABLE,
                ]
            )
            console.print("[green]Claude MCP server installed at user scope.[/green]")
        else:
            console.print("[green]Claude MCP server is already registered at user scope.[/green]")


@app.command("demo")
def demo(
    auto_decide: Annotated[
        bool,
        typer.Option(help="Resolve the deterministic demo without placing a real call."),
    ] = False,
) -> None:
    """Run the deterministic database-RU incident scenario."""

    from .demo import run_demo

    result = asyncio.run(run_demo(auto_decide=auto_decide))
    console.print_json(data=result)


def _checked_run(command: list[str]) -> None:
    resolved_command = _resolve_subprocess_command(command)
    result = subprocess.run(resolved_command, check=False, shell=False)
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


def _captured_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    resolved_command = _resolve_subprocess_command(command)
    return subprocess.run(
        resolved_command,
        check=False,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _uv_tool_install_needed(project_root: Path) -> bool:
    """Return whether the editable uv tool is absent.

    Replacing a uv tool environment from a process running inside that same
    environment is unsafe on Windows. Exact editable installs are therefore a
    no-op, while a different or unreadable existing install is an explicit
    conflict rather than an implicit ``--force`` replacement.
    """

    current_receipt = Path(sys.prefix) / "uv-receipt.toml"
    if current_receipt.is_file():
        current_tool, current_source = _agent_hotline_receipt(current_receipt)
        if current_tool:
            if current_source is not None and _same_path(current_source, project_root):
                return False
            raise typer.BadParameter(
                "install-clients is running from an Agent Hotline uv tool installed "
                "from a different repository. Refusing to replace its active environment; "
                "after this command exits, install the intended repository explicitly."
            )

    result = _captured_run(["uv", "tool", "dir"])
    if result.returncode != 0:
        raise typer.BadParameter("Could not inspect the uv tool directory")
    tool_directory = Path(result.stdout.strip())
    if not result.stdout.strip():
        raise typer.BadParameter("uv returned an empty tool directory")

    receipt = tool_directory / _TOOL_DISTRIBUTION / "uv-receipt.toml"
    if not receipt.is_file():
        return True

    installed_tool, installed_source = _agent_hotline_receipt(receipt)
    if (
        installed_tool
        and installed_source is not None
        and _same_path(installed_source, project_root)
    ):
        return False
    raise typer.BadParameter(
        "Agent Hotline is already installed as a uv tool from another source. "
        "Refusing to replace it automatically; remove or reinstall that tool explicitly."
    )


def _agent_hotline_receipt(receipt_path: Path) -> tuple[bool, Path | None]:
    try:
        payload = tomllib.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise typer.BadParameter(f"Could not read uv tool metadata at {receipt_path}") from exc

    tool = payload.get("tool")
    requirements = tool.get("requirements") if isinstance(tool, dict) else None
    if not isinstance(requirements, list):
        return False, None
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        name = requirement.get("name")
        if not isinstance(name, str) or _normalized_distribution_name(name) != _TOOL_DISTRIBUTION:
            continue
        editable = requirement.get("editable")
        source = Path(editable) if isinstance(editable, str) and editable.strip() else None
        return True, source
    return False, None


def _plan_codex_registration(
    project_root: Path,
    marketplace_name: str,
) -> _CodexRegistrationPlan:
    marketplaces = _command_json(
        ["codex", "plugin", "marketplace", "list", "--json"],
        "Codex marketplaces",
    )
    marketplace_rows = marketplaces.get("marketplaces")
    if not isinstance(marketplace_rows, list):
        raise typer.BadParameter("Codex returned an invalid marketplace list")

    add_marketplace = True
    for row in marketplace_rows:
        if not isinstance(row, dict) or row.get("name") != marketplace_name:
            continue
        root = row.get("root")
        if not isinstance(root, str) or not _same_path(root, project_root):
            raise typer.BadParameter(
                f"Codex marketplace {marketplace_name!r} is already registered from "
                "a different source; refusing to replace it automatically"
            )
        add_marketplace = False
        break

    plugins = _command_json(
        ["codex", "plugin", "list", "--json"],
        "installed Codex plugins",
    )
    plugin_rows = plugins.get("installed")
    if not isinstance(plugin_rows, list):
        raise typer.BadParameter("Codex returned an invalid installed-plugin list")

    plugin_id = f"agent-hotline@{marketplace_name}"
    add_plugin = True
    for row in plugin_rows:
        if not isinstance(row, dict):
            continue
        matches = row.get("pluginId") == plugin_id or (
            row.get("name") == "agent-hotline" and row.get("marketplaceName") == marketplace_name
        )
        if not matches:
            continue
        source = row.get("source")
        source_path = source.get("path") if isinstance(source, dict) else None
        expected_source = project_root / "plugins" / "agent-hotline"
        if isinstance(source_path, str) and not _same_path(source_path, expected_source):
            raise typer.BadParameter(
                f"Codex plugin {plugin_id!r} is installed from a different source; "
                "refusing to replace it automatically"
            )
        add_plugin = False
        break

    return _CodexRegistrationPlan(
        add_marketplace=add_marketplace,
        add_plugin=add_plugin,
    )


def _claude_registration_needed() -> bool:
    result = _captured_run(["claude", "mcp", "get", "agent-hotline"])
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        if "no mcp server named" in combined.casefold():
            return True
        raise typer.BadParameter("Could not inspect the existing Claude MCP registration")

    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.strip().partition(":")
        if separator:
            fields[key.casefold()] = value.strip()

    scope = fields.get("scope", "").casefold()
    transport = fields.get("type", "").casefold()
    command = fields.get("command", "")
    args = fields.get("args", "")
    if (
        scope.startswith("user config")
        and transport == "stdio"
        and command == _MCP_EXECUTABLE
        and not args
    ):
        return False
    raise typer.BadParameter(
        "Claude already has an MCP server named 'agent-hotline' with a different "
        "scope or command; refusing to overwrite it automatically"
    )


def _command_json(command: list[str], description: str) -> dict[str, object]:
    result = _captured_run(command)
    if result.returncode != 0:
        raise typer.BadParameter(f"Could not inspect {description}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"{description} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{description} returned an invalid response")
    return payload


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    def normalized(value: str | os.PathLike[str]) -> str:
        path = os.path.realpath(os.path.abspath(os.fspath(value)))
        if path.startswith("\\\\?\\UNC\\"):
            path = "\\\\" + path[8:]
        elif path.startswith("\\\\?\\"):
            path = path[4:]
        return os.path.normcase(os.path.normpath(path))

    return normalized(left) == normalized(right)


def _normalized_distribution_name(name: str) -> str:
    return name.casefold().replace("_", "-").replace(".", "-")


def _resolve_subprocess_command(command: list[str]) -> list[str]:
    """Resolve cross-platform CLI shims without enabling a command shell."""

    if not command:
        raise ValueError("command cannot be empty")

    executable = command[0]
    if platform.system() != "Windows":
        return [shutil.which(executable) or executable, *command[1:]]

    # Respect PATH ordering so an npm .cmd shim is not silently replaced by an
    # unrelated, stale executable later on PATH. If this installation ships a
    # native sibling executable, prefer that exact sibling.
    resolved = shutil.which(executable) or executable
    suffix = Path(resolved).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        sibling_exe = Path(resolved).with_suffix(".exe")
        if sibling_exe.is_file():
            resolved = str(sibling_exe)
    if Path(resolved).suffix.lower() not in {".cmd", ".bat"}:
        return [resolved, *command[1:]]

    comspec = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not comspec:
        raise RuntimeError("cmd.exe is required to invoke a Windows command shim")
    command_line = subprocess.list2cmdline([resolved, *command[1:]])
    return [comspec, "/d", "/c", command_line]


def _read_marketplace_name(repository_root: Path) -> str:
    marketplace_path = repository_root / ".agents" / "plugins" / "marketplace.json"
    try:
        payload = json.loads(marketplace_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise typer.BadParameter(
            f"Codex marketplace manifest is missing: {marketplace_path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(
            f"Codex marketplace manifest is unreadable: {marketplace_path}"
        ) from exc

    name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise typer.BadParameter(
            f"Codex marketplace manifest has no valid name: {marketplace_path}"
        )
    return name.strip()


def _read_windows_user_env(name: str) -> str | None:
    if platform.system() != "Windows":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
        return value if isinstance(value, str) else None
    except OSError:
        return None


def _display_value(value: object) -> str:
    if isinstance(value, bool):
        return "[green]yes[/green]" if value else "[yellow]no[/yellow]"
    return json.dumps(value) if isinstance(value, (dict, list)) else str(value)


if __name__ == "__main__":
    app()
