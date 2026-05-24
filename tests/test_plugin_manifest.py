"""Validate the Claude Code plugin manifest + bundle structure.

Catches:
  - Missing / malformed `.claude-plugin/plugin.json`.
  - Required manifest fields gone missing.
  - Skill / subagent paths the plugin advertises but doesn't ship.
  - `.mcp.json` server entry drifting away from the package's actual
    Python entry-point.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_DIR = _REPO_ROOT / ".claude-plugin"
_MANIFEST = _PLUGIN_DIR / "plugin.json"
_MCP_CONFIG = _REPO_ROOT / ".mcp.json"
_SKILL = _REPO_ROOT / "skills" / "architectural-drafting" / "SKILL.md"
_AGENT = _REPO_ROOT / "agents" / "autocad-architect.md"


# ---------------------------------------------------------------------------
# Manifest schema
# ---------------------------------------------------------------------------


class TestPluginManifest:
    @pytest.fixture
    def manifest(self) -> dict:
        return json.loads(_MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_file_exists(self):
        assert _MANIFEST.exists(), (
            f"Missing plugin manifest: {_MANIFEST}. "
            "Claude Code expects `.claude-plugin/plugin.json` at the repo root."
        )

    def test_required_fields_present(self, manifest):
        for field in ("name", "description"):
            assert field in manifest, f"plugin.json missing required field: {field}"
            assert manifest[field], f"plugin.json field is empty: {field}"

    def test_name_is_kebab_case(self, manifest):
        # Claude Code namespaces skills under <plugin-name>:<skill> — kebab-case
        # makes the namespace addressable cleanly in slash commands.
        name = manifest["name"]
        assert name == name.lower()
        assert " " not in name
        assert "_" not in name, f"Use hyphens, not underscores: {name!r}"

    def test_version_matches_pyproject(self, manifest):
        """The plugin's advertised version should track pyproject.toml AND
        the Python package's `__version__` constant. All three must agree so
        the marketplace listing, the installed wheel's metadata, and the
        runtime `autocad_mcp.__version__` import never lie."""
        import sys

        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib  # type: ignore[import-not-found]

        with (_REPO_ROOT / "pyproject.toml").open("rb") as fp:
            py = tomllib.load(fp)
        py_version = py["project"]["version"]
        assert manifest.get("version") == py_version, (
            f"plugin.json version {manifest.get('version')!r} != "
            f"pyproject.toml version {py_version!r}"
        )

        import autocad_mcp
        assert autocad_mcp.__version__ == py_version, (
            f"autocad_mcp.__version__ {autocad_mcp.__version__!r} != "
            f"pyproject.toml version {py_version!r}"
        )

    def test_description_substantive(self, manifest):
        # 50+ chars catches the "TODO" placeholder + the unhelpful one-word case.
        assert len(manifest["description"]) >= 50


# ---------------------------------------------------------------------------
# Bundle structure
# ---------------------------------------------------------------------------


class TestPluginBundle:
    def test_skill_shipped_at_plugin_root(self):
        assert _SKILL.exists(), (
            f"Plugin advertises the architectural-drafting skill but "
            f"{_SKILL} doesn't exist. The skill must live under "
            "<plugin-root>/skills/<skill-name>/SKILL.md, not under .claude/."
        )

    def test_agent_shipped_at_plugin_root(self):
        assert _AGENT.exists(), (
            f"Plugin advertises the autocad-architect subagent but "
            f"{_AGENT} doesn't exist."
        )

    def test_mcp_config_exists(self):
        assert _MCP_CONFIG.exists(), (
            f"Missing {_MCP_CONFIG}. The MCP server config can be inline in "
            "plugin.json or in a sibling `.mcp.json` — this plugin uses the "
            "sibling file."
        )

    def test_mcp_config_declares_autocad_server(self):
        config = json.loads(_MCP_CONFIG.read_text(encoding="utf-8"))
        assert "mcpServers" in config
        servers = config["mcpServers"]
        assert "autocad" in servers, (
            f"Expected an 'autocad' entry in mcpServers; got {list(servers.keys())}"
        )

    def test_mcp_command_resolves_against_plugin_root(self):
        """The MCP server config must reference `${CLAUDE_PLUGIN_ROOT}` so
        plugin install / move doesn't break the launch.  The current shape
        uses `uv run --project ${CLAUDE_PLUGIN_ROOT}` rather than a hard-coded
        venv path, but the placeholder still has to appear somewhere in the
        args."""
        config = json.loads(_MCP_CONFIG.read_text(encoding="utf-8"))
        entry = config["mcpServers"]["autocad"]
        combined = entry["command"] + " " + " ".join(entry["args"])
        assert "${CLAUDE_PLUGIN_ROOT}" in combined, (
            f"Neither command nor args reference ${{CLAUDE_PLUGIN_ROOT}}: "
            f"{entry!r}"
        )

    def test_mcp_command_invokes_autocad_mcp_module(self):
        """The args list must end with `python -m autocad_mcp` (or equivalent)
        so the server actually launches our entry point."""
        config = json.loads(_MCP_CONFIG.read_text(encoding="utf-8"))
        args = config["mcpServers"]["autocad"]["args"]
        # Tolerant match: the final three args should be `python -m autocad_mcp`.
        assert args[-3:] == ["python", "-m", "autocad_mcp"], (
            f"Expected args to end with `python -m autocad_mcp`; got {args!r}"
        )

    def test_mcp_command_is_cross_platform(self):
        """The command must not hard-code Windows-only paths like
        `.venv/Scripts/python.exe` — a Mac/Linux user installing the plugin
        for the ezdxf headless backend would fail otherwise."""
        config = json.loads(_MCP_CONFIG.read_text(encoding="utf-8"))
        entry = config["mcpServers"]["autocad"]
        combined = entry["command"] + " " + " ".join(entry["args"])
        # The Windows venv-layout string is the canonical OS-locked path; if it
        # appears, the plugin won't run on Mac/Linux.
        assert "Scripts/python.exe" not in combined
        assert "Scripts\\python.exe" not in combined
