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
from .settings import Settings, get_settings, reset_settings_cache, runtime_env_path

app = typer.Typer(
    name="agent-hotline",
    help="Voice control plane for Codex and Claude agents.",
    no_args_is_help=True,
)
console = Console()

_TOOL_DISTRIBUTION = "agent-hotline"
_MCP_EXECUTABLE = "agent-hotline-mcp"
_MANAGED_SECRET_NAMES = (
    "HOTLINE_LOCAL_TOKEN",
    "HOTLINE_SIP_CORRELATION_SECRET",
    "HOTLINE_ACTION_SIGNING_SECRET",
    "HOTLINE_FALLBACK_SIGNING_SECRET",
    "HOTLINE_FALLBACK_WEBHOOK_TOKEN",
    "VAPI_WEBHOOK_TOKEN",
)
_TERMINAL_RESULT_STATUSES = frozenset(
    {
        "resolved",
        "deferred",
        "no_answer",
        "busy",
        "failed",
        "timed_out",
    }
)


@dataclass(frozen=True)
class _CodexRegistrationPlan:
    add_marketplace: bool
    add_plugin: bool


@dataclass(frozen=True)
class _ClaudeRegistrationPlan:
    remove_existing: bool
    add_server: bool


@app.command("serve")
def serve(
    host: Annotated[str | None, typer.Option(help="Bind host override.")] = None,
    port: Annotated[int | None, typer.Option(help="Bind port override.")] = None,
    reload: Annotated[bool, typer.Option(help="Enable development reload.")] = False,
) -> None:
    """Run the persistent Hotline daemon."""

    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "agent_hotline.api:create_app",
        factory=True,
        host=host or settings.hotline_host,
        port=port or settings.hotline_port,
        reload=reload,
        log_level=settings.hotline_log_level.lower(),
        # Public provider routes are authenticated with signature headers. Access
        # logging remains disabled so correlation metadata is not copied into
        # terminal history.
        access_log=False,
    )


@app.command("doctor")
def doctor(
    live: Annotated[
        bool,
        typer.Option(help="Also query the local daemon and selected provider APIs."),
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


async def _doctor_live(settings: Settings) -> int:
    exit_code = 0
    try:
        async with HotlineClient() as client:
            health = await client.health()
        console.print("[green]Local daemon:[/green] healthy")
        console.print_json(data=health.model_dump(mode="json"))
    except HotlineClientError as exc:
        console.print(f"[yellow]Local daemon:[/yellow] {exc}")
        exit_code = 1

    typed_settings = settings
    if typed_settings.hotline_transport == "openai_realtime":
        from .openai_realtime import (
            OpenAIRealtimeAPIError,
            OpenAIRealtimeClient,
        )
        from .twilio import TwilioAPIError, TwilioClient
        from .vobiz import VobizAPIError, VobizClient

        if typed_settings.openai_api_key.get_secret_value():
            try:
                async with OpenAIRealtimeClient(typed_settings) as client:
                    await client.probe()
                console.print("[green]OpenAI Realtime API:[/green] reachable")
            except (OpenAIRealtimeAPIError, ValueError) as exc:
                status_code = getattr(exc, "status_code", None)
                console.print(
                    "[yellow]OpenAI Realtime API:[/yellow] "
                    f"{exc} (status={status_code or 'network'})"
                )
                exit_code = 1
        else:
            console.print("[yellow]OpenAI Realtime API:[/yellow] key not configured")
            exit_code = 1

        if typed_settings.hotline_carrier == "vobiz" and typed_settings.vobiz_configured:
            try:
                async with VobizClient(typed_settings) as client:
                    await client.probe()
                console.print("[green]Vobiz API:[/green] reachable")
            except (VobizAPIError, ValueError) as exc:
                status_code = getattr(exc, "status_code", None)
                console.print(
                    f"[yellow]Vobiz API:[/yellow] {exc} (status={status_code or 'network'})"
                )
                exit_code = 1
        elif typed_settings.hotline_carrier == "vobiz":
            console.print("[yellow]Vobiz API:[/yellow] configuration incomplete")
            exit_code = 1
        elif typed_settings.twilio_configured:
            try:
                async with TwilioClient(typed_settings) as client:
                    await client.probe()
                console.print("[green]Twilio API:[/green] reachable")
            except (TwilioAPIError, ValueError) as exc:
                status_code = getattr(exc, "status_code", None)
                console.print(
                    f"[yellow]Twilio API:[/yellow] {exc} (status={status_code or 'network'})"
                )
                exit_code = 1
        else:
            console.print("[yellow]Twilio API:[/yellow] configuration incomplete")
            exit_code = 1
        return exit_code

    console.print(
        f"[yellow]Provider API:[/yellow] no live probe for {typed_settings.hotline_transport!r}"
    )
    return exit_code


@app.command("init-secrets")
def init_secrets(
    force: Annotated[
        bool,
        typer.Option(help="Rotate existing Hotline-only tokens."),
    ] = False,
) -> None:
    """Generate local service tokens without displaying them."""

    runtime_file = runtime_env_path()
    existing_runtime_text = ""
    runtime_values: dict[str, str] = {}
    if platform.system() != "Windows" and runtime_file.exists():
        existing_runtime_text = runtime_file.read_text(encoding="utf-8")
        runtime_values = _read_managed_runtime_values(existing_runtime_text)

    generated: dict[str, str] = {}
    for name in _MANAGED_SECRET_NAMES:
        if platform.system() == "Windows":
            existing = os.environ.get(name) or _read_windows_user_env(name)
        else:
            existing = runtime_values.get(name) or os.environ.get(name)
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
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_private_text(
            runtime_file,
            _merge_managed_runtime_values(existing_runtime_text, generated),
        )
        destination = str(runtime_file)

    reset_settings_cache()
    console.print(f"[green]Stored five Hotline service tokens in {destination}.[/green]")
    console.print("No token values were printed.")


def _read_managed_runtime_values(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    managed = set(_MANAGED_SECRET_NAMES)
    for line in content.splitlines():
        assignment = _parse_runtime_assignment(line)
        if assignment is None:
            continue
        name, value = assignment
        if name in managed and value:
            values[name] = value
    return values


def _merge_managed_runtime_values(
    content: str,
    values: dict[str, str],
) -> str:
    """Replace only Hotline-managed keys while preserving every unrelated line."""

    output: list[str] = []
    written: set[str] = set()
    for line in content.splitlines():
        assignment = _parse_runtime_assignment(line)
        name = assignment[0] if assignment is not None else None
        if name in values:
            if name not in written:
                output.append(f"{name}={values[name]}")
                written.add(name)
            continue
        output.append(line)
    for name in _MANAGED_SECRET_NAMES:
        if name not in written:
            output.append(f"{name}={values[name]}")
    return "\n".join(output) + "\n"


def _parse_runtime_assignment(line: str) -> tuple[str, str] | None:
    candidate = line.strip()
    if not candidate or candidate.startswith("#"):
        return None
    if candidate.startswith("export "):
        candidate = candidate[7:].lstrip()
    name, separator, raw_value = candidate.partition("=")
    name = name.strip()
    if not separator or not name.replace("_", "").isalnum() or not name[0].isalpha():
        return None
    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    return name, value


def _atomic_write_private_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


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
        wait_for_decision=True,
        timeout_seconds=timeout,
    )
    result = asyncio.run(_contact(request, start_only=no_wait))
    console.print_json(data=result)


async def _contact(
    request: ContactHumanRequest,
    *,
    start_only: bool = False,
) -> dict[str, object]:
    async with HotlineClient() as client:
        result = (
            await client.start_contact_human(request)
            if start_only
            else await client.contact_human(request)
        )
    return result.model_dump(mode="json", exclude_none=True)


@app.command("result")
def result(
    event_id: Annotated[str, typer.Argument(help="Durable evt_... identifier.")],
    watch: Annotated[
        bool,
        typer.Option(help="Poll until a terminal result or the local watch timeout."),
    ] = False,
    timeout: Annotated[
        int,
        typer.Option(min=1, max=7200, help="Maximum local watch time in seconds."),
    ] = 600,
    interval: Annotated[
        float,
        typer.Option(min=0.2, max=30.0, help="Polling interval while watching."),
    ] = 1.0,
) -> None:
    """Read or watch one durable escalation result."""

    payload, terminal = asyncio.run(
        _event_result(
            event_id,
            watch=watch,
            timeout_seconds=timeout,
            interval_seconds=interval,
        )
    )
    console.print_json(data=payload)
    if watch and not terminal:
        raise typer.Exit(code=2)


async def _event_result(
    event_id: str,
    *,
    watch: bool,
    timeout_seconds: float,
    interval_seconds: float,
) -> tuple[dict[str, object], bool]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with HotlineClient() as client:
        while True:
            result = await client.get_result(event_id)
            terminal = result.status in _TERMINAL_RESULT_STATUSES
            if terminal or not watch:
                return result.model_dump(mode="json", exclude_none=True), terminal
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return result.model_dump(mode="json", exclude_none=True), False
            await asyncio.sleep(min(interval_seconds, remaining))


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
        Path | None,
        typer.Option(
            help=(
                "Marketplace root override. By default, use the source checkout when "
                "available or the plugin bundled in the installed wheel."
            )
        ),
    ] = None,
) -> None:
    """Install or locate the CLI and register Codex/Claude integrations."""

    resolved_marketplace_root = _resolve_marketplace_root(marketplace_root)
    editable_root = (
        resolved_marketplace_root
        if (resolved_marketplace_root / "pyproject.toml").is_file()
        else None
    )

    marketplace_name: str | None = None
    if client in {"codex", "all"}:
        marketplace_name = _read_marketplace_name(resolved_marketplace_root)

    install_tool = _uv_tool_install_needed(editable_root)
    codex_plan: _CodexRegistrationPlan | None = None
    if client in {"codex", "all"}:
        assert marketplace_name is not None
        codex_plan = _plan_codex_registration(resolved_marketplace_root, marketplace_name)
    claude_plan = (
        _plan_claude_registration()
        if client in {"claude", "all"}
        else _ClaudeRegistrationPlan(remove_existing=False, add_server=False)
    )

    if install_tool:
        assert editable_root is not None
        _checked_run(["uv", "tool", "install", "--editable", str(editable_root)])
    else:
        console.print("[green]Agent Hotline command suite is already installed.[/green]")

    if client in {"codex", "all"}:
        assert marketplace_name is not None
        assert codex_plan is not None
        if codex_plan.add_marketplace:
            _checked_run(
                [
                    "codex",
                    "plugin",
                    "marketplace",
                    "add",
                    str(resolved_marketplace_root),
                ]
            )
        if codex_plan.add_plugin:
            _checked_run(["codex", "plugin", "add", f"agent-hotline@{marketplace_name}"])
        if codex_plan.add_marketplace or codex_plan.add_plugin:
            console.print("[green]Codex plugin installed.[/green] Start a new task to load it.")
        else:
            console.print("[green]Codex plugin is already registered.[/green]")

    if client in {"claude", "all"}:
        if claude_plan.remove_existing:
            _checked_run(
                [
                    "claude",
                    "mcp",
                    "remove",
                    "--scope",
                    "user",
                    "agent-hotline",
                ]
            )
        if claude_plan.add_server:
            _checked_run(
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
    try:
        result = subprocess.run(resolved_command, check=False, shell=False)
    except OSError as exc:
        raise typer.BadParameter(f"Required executable is unavailable: {command[0]}") from exc
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


def _captured_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    resolved_command = _resolve_subprocess_command(command)
    try:
        return subprocess.run(
            resolved_command,
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise typer.BadParameter(f"Required executable is unavailable: {command[0]}") from exc


def _uv_tool_install_needed(project_root: Path | None) -> bool:
    """Return whether an editable checkout still needs a persistent uv tool.

    Replacing a uv tool environment from a process running inside that same
    environment is unsafe on Windows. Exact editable installs are therefore a
    no-op, while a different or unreadable existing install is an explicit
    conflict rather than an implicit ``--force`` replacement. A release-wheel
    invocation has no editable project root and must already expose the bundled
    ``agent-hotline-mcp`` companion command.
    """

    current_receipt = Path(sys.prefix) / "uv-receipt.toml"
    if current_receipt.is_file():
        current_tool, current_source = _agent_hotline_receipt(current_receipt)
        if current_tool:
            if (
                project_root is not None
                and current_source is not None
                and _same_path(current_source, project_root)
            ):
                return False
            if project_root is None and _is_persistent_uv_tool_environment():
                return False
            raise typer.BadParameter(
                "install-clients is running from an Agent Hotline uv tool installed "
                "from a different source or a temporary tool environment. Refusing to "
                "replace its active environment; after this command exits, install the "
                "intended release or repository explicitly with uv tool install."
            )

    if project_root is None:
        if shutil.which(_MCP_EXECUTABLE):
            return False
        raise typer.BadParameter(
            "The bundled plugin is available, but agent-hotline-mcp is not on PATH. "
            "Install the release persistently with `uv tool install agent-hotline`, "
            "then run install-clients again."
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


def _is_persistent_uv_tool_environment() -> bool:
    result = _captured_run(["uv", "tool", "dir"])
    if result.returncode != 0 or not result.stdout.strip():
        return False
    expected_environment = Path(result.stdout.strip()) / _TOOL_DISTRIBUTION
    return _same_path(sys.prefix, expected_environment)


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
    expected_plugin_version = _read_plugin_version(project_root)
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
        row_plugin_id = row.get("pluginId")
        row_name = row.get("name")
        row_marketplace = row.get("marketplaceName")
        is_hotline = row_name == "agent-hotline" or (
            isinstance(row_plugin_id, str) and row_plugin_id.startswith("agent-hotline@")
        )
        is_other_marketplace = is_hotline and (
            row_plugin_id != plugin_id
            and not (row_name == "agent-hotline" and row_marketplace == marketplace_name)
        )
        if is_other_marketplace:
            installed_id = (
                row_plugin_id
                if isinstance(row_plugin_id, str)
                else f"agent-hotline@{row_marketplace or 'unknown'}"
            )
            raise typer.BadParameter(
                f"Codex already has {installed_id!r} installed. Agent Hotline does "
                "not remove plugin registrations automatically; remove the old entry "
                f"with `codex plugin remove {installed_id}` and rerun install-clients."
            )
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
        add_plugin = row.get("version") != expected_plugin_version
        break

    return _CodexRegistrationPlan(
        add_marketplace=add_marketplace,
        add_plugin=add_plugin,
    )


def _plan_claude_registration() -> _ClaudeRegistrationPlan:
    result = _captured_run(["claude", "mcp", "get", "agent-hotline"])
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        if "no mcp server named" in combined.casefold():
            return _ClaudeRegistrationPlan(remove_existing=False, add_server=True)
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
    environment = fields.get("environment", "")
    has_client_identity = environment.casefold() == "hotline_mcp_client=claude" or any(
        line.strip().casefold()
        in {
            "hotline_mcp_client=claude",
            "hotline_mcp_client: claude",
        }
        for line in result.stdout.splitlines()
    )
    base_registration_matches = (
        scope.startswith("user config")
        and transport == "stdio"
        and command == _MCP_EXECUTABLE
        and not args
    )
    if base_registration_matches and has_client_identity:
        return _ClaudeRegistrationPlan(remove_existing=False, add_server=False)
    if base_registration_matches and not environment:
        return _ClaudeRegistrationPlan(remove_existing=True, add_server=True)
    raise typer.BadParameter(
        "Claude already has an MCP server named 'agent-hotline' with a different "
        "scope, command, or HOTLINE_MCP_CLIENT identity; refusing to overwrite it "
        "automatically"
    )


def _claude_registration_needed() -> bool:
    """Backward-compatible predicate used by external installer checks."""

    return _plan_claude_registration().add_server


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


def _read_plugin_version(marketplace_root: Path) -> str:
    plugin_path = marketplace_root / "plugins" / "agent-hotline" / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(plugin_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"Codex plugin manifest is unreadable: {plugin_path}") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise typer.BadParameter(f"Codex plugin manifest has no valid version: {plugin_path}")
    return version.strip()


def _resolve_marketplace_root(requested_root: Path | None) -> Path:
    if requested_root is not None:
        resolved = requested_root.resolve()
        _validate_hotline_marketplace_root(resolved)
        return resolved

    source_root = Path(__file__).resolve().parents[2]
    bundled_root = Path(__file__).resolve().parent / "_distribution"
    candidates = [
        source_root,
        bundled_root,
        Path.cwd().resolve(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        marker = os.path.normcase(os.path.normpath(str(candidate)))
        if marker in seen:
            continue
        seen.add(marker)
        try:
            _validate_hotline_marketplace_root(candidate)
        except typer.BadParameter:
            continue
        return candidate

    raise typer.BadParameter(
        "Could not locate the Agent Hotline Codex plugin. Reinstall a complete "
        "Agent Hotline wheel or pass --marketplace-root pointing at a source checkout."
    )


def _validate_hotline_marketplace_root(root: Path) -> None:
    marketplace_path = root / ".agents" / "plugins" / "marketplace.json"
    plugin_manifest_path = root / "plugins" / "agent-hotline" / ".codex-plugin" / "plugin.json"
    try:
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        plugin = json.loads(plugin_manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        missing = Path(exc.filename) if exc.filename else marketplace_path
        raise typer.BadParameter(f"Codex plugin bundle is incomplete: {missing}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise typer.BadParameter(f"Codex plugin bundle is unreadable under {root}") from exc

    plugin_name = plugin.get("name") if isinstance(plugin, dict) else None
    rows = marketplace.get("plugins") if isinstance(marketplace, dict) else None
    matching_row = (
        next(
            (row for row in rows if isinstance(row, dict) and row.get("name") == "agent-hotline"),
            None,
        )
        if isinstance(rows, list)
        else None
    )
    expected_source = {"source": "local", "path": "./plugins/agent-hotline"}
    if plugin_name != "agent-hotline" or not isinstance(matching_row, dict):
        raise typer.BadParameter(f"{root} is not an Agent Hotline marketplace")
    if matching_row.get("source") != expected_source:
        raise typer.BadParameter("Agent Hotline marketplace source must be ./plugins/agent-hotline")


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
