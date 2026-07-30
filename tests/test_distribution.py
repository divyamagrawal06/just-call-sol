"""Build and inspect release artifacts as part of the test suite."""

from __future__ import annotations

import configparser
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_REMOVED_ARTIFACT_MARKERS = (
    "sarvam",
    "samvaad",
    "event_tracker",
    "hackathon",
    "registration_tracker",
    ".csv",
)


def _assert_no_removed_voice_or_demo_artifacts(names: set[str]) -> None:
    offenders = sorted(
        name
        for name in names
        if any(marker in name.lower() for marker in _REMOVED_ARTIFACT_MARKERS)
    )
    assert offenders == []


def test_wheel_and_sdist_contain_runtime_and_integration_assets(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to validate release artifacts")

    repository_root = Path(__file__).resolve().parents[1]
    output_dir = tmp_path / "dist"
    subprocess.run(
        [uv, "build", "--out-dir", str(output_dir)],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )

    wheel = next(output_dir.glob("agent_hotline-*.whl"))
    sdist = next(output_dir.glob("agent_hotline-*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        _assert_no_removed_voice_or_demo_artifacts(names)
        assert "agent_hotline/mcp_server.py" in names
        assert "agent_hotline/cli.py" in names
        assert "agent_hotline/py.typed" in names
        assert "agent_hotline/dashboard_assets/index.html" in names
        assert "agent_hotline/fallback_assets/index.html" in names
        assert "agent_hotline/fallback_assets/fallback.js" in names
        assert "agent_hotline/_distribution/.agents/plugins/marketplace.json" in names
        assert (
            "agent_hotline/_distribution/plugins/agent-hotline/.codex-plugin/plugin.json" in names
        )
        assert "agent_hotline/_distribution/plugins/agent-hotline/.mcp.json" in names
        assert (
            "agent_hotline/_distribution/plugins/agent-hotline/"
            "skills/agent-hotline/SKILL.md" in names
        )
        assert (
            "agent_hotline/_distribution/plugins/agent-hotline/"
            "skills/agent-hotline/references/policy.md" in names
        )
        assert (
            "agent_hotline/_distribution/plugins/agent-hotline/"
            "skills/agent-hotline/agents/openai.yaml" in names
        )

        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        parser = configparser.ConfigParser()
        parser.read_string(archive.read(entry_points_name).decode())
        console_scripts = dict(parser["console_scripts"])
        assert console_scripts["agent-hotline"] == "agent_hotline.cli:app"
        assert console_scripts["agent-hotline-claude-hook"] == "agent_hotline.claude_hooks:main"
        assert console_scripts["agent-hotline-mcp"] == "agent_hotline.mcp_server:main"
        assert set(console_scripts) == {
            "agent-hotline",
            "agent-hotline-claude-hook",
            "agent-hotline-mcp",
        }

        extracted = tmp_path / "extracted-wheel"
        archive.extractall(extracted)

    import_check = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import agent_hotline; "
                "from agent_hotline.cli import app; "
                "from agent_hotline.cli import _resolve_marketplace_root; "
                "from agent_hotline.mcp_server import mcp; "
                "root = _resolve_marketplace_root(None); "
                "assert str(root).startswith(sys.argv[1]); "
                "assert (root / 'plugins/agent-hotline/.mcp.json').is_file(); "
                "assert app.info.name == 'agent-hotline'; "
                "assert mcp.name == 'agent-hotline'"
            ),
            str(extracted),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(extracted)},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert import_check.returncode == 0, import_check.stderr

    with tarfile.open(sdist, "r:gz") as archive:
        relative_names = {"/".join(Path(name).parts[1:]) for name in archive.getnames()}
        _assert_no_removed_voice_or_demo_artifacts(relative_names)
        assert "plugins/agent-hotline/.codex-plugin/plugin.json" in relative_names
        assert "plugins/agent-hotline/.mcp.json" in relative_names
        assert "integrations/claude/settings.example.json" in relative_names


def test_plugin_and_marketplace_manifests_are_consistent() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    plugin_root = repository_root / "plugins" / "agent-hotline"
    plugin = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (repository_root / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )

    assert plugin["name"] == plugin_root.name == marketplace["plugins"][0]["name"]
    assert plugin["mcpServers"] == "./.mcp.json"
    assert plugin["skills"] == "./skills/"
    assert marketplace["name"] == "agent-hotline-local"
    assert marketplace["plugins"][0]["source"] == {
        "source": "local",
        "path": "./plugins/agent-hotline",
    }
    assert marketplace["plugins"][0]["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
