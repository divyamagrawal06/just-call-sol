from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugins" / "agent-hotline"
SKILL_ROOT = PLUGIN_ROOT / "skills" / "agent-hotline"


def _read_frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(?P<frontmatter>.*?)\n---", text, flags=re.DOTALL)
    assert match is not None

    values: dict[str, str] = {}
    for line in match.group("frontmatter").splitlines():
        key, separator, value = line.partition(":")
        assert separator
        values[key.strip()] = value.strip().strip('"')
    return values


def _read_openai_interface(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("interface:\n")

    values: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        match = re.fullmatch(r'  ([a-z_]+): "(.*)"', line)
        assert match is not None
        values[match.group(1)] = match.group(2)
    return values


def test_plugin_bundle_identity_and_version_are_consistent() -> None:
    plugin = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (REPOSITORY_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    skill = _read_frontmatter(SKILL_ROOT / "SKILL.md")
    openai_interface = _read_openai_interface(SKILL_ROOT / "agents" / "openai.yaml")

    marketplace_plugin = next(
        entry for entry in marketplace["plugins"] if entry["name"] == plugin["name"]
    )

    assert plugin["name"] == SKILL_ROOT.name == skill["name"] == "agent-hotline"
    assert plugin["version"] == project["project"]["version"] == "0.2.0+codex.20260824132732"
    assert plugin["description"] == marketplace_plugin["description"]
    assert marketplace_plugin["source"] == {
        "source": "local",
        "path": "./plugins/agent-hotline",
    }
    assert plugin["skills"] == "./skills/"
    assert plugin["mcpServers"] == "./.mcp.json"
    assert openai_interface["display_name"] == plugin["interface"]["displayName"]
    assert 25 <= len(openai_interface["short_description"]) <= 64
    assert "$agent-hotline" in openai_interface["default_prompt"]
    assert set(openai_interface) == {
        "display_name",
        "short_description",
        "default_prompt",
    }


def test_plugin_bundles_a_synchronous_bounded_permission_hook() -> None:
    hook_config = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    permission_groups = hook_config["hooks"]["PermissionRequest"]

    assert len(permission_groups) == 1
    assert permission_groups[0]["matcher"] == "^Bash$|^mcp__"
    handlers = permission_groups[0]["hooks"]
    assert handlers == [
        {
            "type": "command",
            "command": "agent-hotline-codex-hook",
            "commandWindows": "agent-hotline-codex-hook",
            "timeout": 660,
            "statusMessage": "Calling your owner for approval",
        }
    ]
    assert "async" not in handlers[0]


def test_skill_bundle_is_realtime_twilio_first_conversational_and_safe() -> None:
    plugin_text = (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    policy_text = (SKILL_ROOT / "references" / "policy.md").read_text(encoding="utf-8")
    core_bundle = "\n".join((plugin_text, skill_text, policy_text)).lower()

    for required in (
        "openai realtime",
        "twilio",
        "natural",
        "conversation",
        "barge-in",
        "exact readback",
        "owner pin",
        "codex",
        "claude",
        "model output as approval",
        "raw shell",
    ):
        assert required in core_bundle

    for stale in (
        "sarvam",
        "samvaad",
        "epoch",
        "check_event_registration",
        "search_event_registrations",
        ".csv",
    ):
        assert stale not in core_bundle
