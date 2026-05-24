"""Validate that the `architectural-drafting` Claude Code skill is loadable.

Claude Code's skill discovery expects:
  - The file at <skill-dir>/SKILL.md exists.
  - The file starts with a YAML frontmatter block delimited by `---`.
  - The frontmatter has at minimum `name` and `description` keys.
  - The `name` matches the containing directory.

These checks are cheap and catch a real failure mode: editing the SKILL.md
and breaking the frontmatter would silently disable the skill in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent

SKILL_DIR = _REPO_ROOT / "skills" / "architectural-drafting"
SKILL_FILE = SKILL_DIR / "SKILL.md"
TEMPLATE_FILE = SKILL_DIR / "templates" / "sample-acadproj.toml"

SUBAGENT_FILE = _REPO_ROOT / "agents" / "autocad-architect.md"


def _parse_frontmatter(text: str) -> dict[str, str | list[str]]:
    """Parse a minimal YAML frontmatter block.

    Supports:
      - scalar `key: value` lines
      - folded scalar continuations (indented lines after an empty value)
      - simple bullet lists (`  - item` lines after a key with empty value)

    We deliberately avoid pulling in PyYAML — frontmatter for Claude Code
    skills / subagents is shape-stable, and a strict parser catches malformed
    inputs (e.g. a missing `---` terminator) that PyYAML would accept silently.
    """
    if not text.startswith("---\n"):
        raise ValueError("File must start with a YAML frontmatter block")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError("Frontmatter block is unterminated")
    block = text[4:end]
    out: dict[str, str | list[str]] = {}
    current_key: str | None = None
    for line in block.splitlines():
        if not line.strip():
            continue
        # Bullet list item: must follow a key whose scalar value is empty.
        bullet = re.match(r"^\s+-\s+(.*)$", line)
        if bullet and current_key is not None:
            existing = out.get(current_key, "")
            if isinstance(existing, str) and existing == "":
                out[current_key] = [bullet.group(1).strip()]
            elif isinstance(existing, list):
                existing.append(bullet.group(1).strip())
            else:
                raise ValueError(
                    f"Bullet under non-empty scalar key {current_key!r}: {line!r}"
                )
            continue

        m = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
        if m:
            current_key = m.group(1)
            out[current_key] = m.group(2).strip()
        elif current_key is not None and line.startswith(" ") and isinstance(out.get(current_key), str):
            # Continuation line for a folded scalar — append.
            existing = out[current_key]
            assert isinstance(existing, str)
            out[current_key] = (existing + " " + line.strip()).strip()
        else:
            raise ValueError(f"Malformed frontmatter line: {line!r}")
    return out


class TestSkillDiscovery:
    def test_skill_file_exists(self):
        assert SKILL_FILE.exists(), f"Missing {SKILL_FILE}"

    def test_template_exists(self):
        assert TEMPLATE_FILE.exists(), f"Missing {TEMPLATE_FILE}"

    def test_frontmatter_parses(self):
        fm = _parse_frontmatter(SKILL_FILE.read_text(encoding="utf-8"))
        assert "name" in fm
        assert "description" in fm

    def test_name_matches_directory(self):
        fm = _parse_frontmatter(SKILL_FILE.read_text(encoding="utf-8"))
        assert fm["name"] == SKILL_DIR.name, (
            f"Frontmatter name={fm['name']!r} doesn't match directory {SKILL_DIR.name!r}"
        )

    def test_description_is_substantive(self):
        fm = _parse_frontmatter(SKILL_FILE.read_text(encoding="utf-8"))
        # 30+ chars covers the "no, really, write a description" case.
        assert len(fm["description"]) >= 30, fm["description"]


class TestSkillContent:
    """Lightweight assertions that the body covers the workflows we promised."""

    def test_body_mentions_setup_layers(self):
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert "setup_layers" in body

    def test_body_mentions_wall_with_openings(self):
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert "wall_with_openings" in body

    def test_body_mentions_check_standard(self):
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert "check_standard" in body

    def test_body_mentions_xrefs(self):
        body = SKILL_FILE.read_text(encoding="utf-8")
        assert "list_xrefs" in body or "find_broken_xrefs" in body

    def test_body_has_pre_commit_checklist(self):
        body = SKILL_FILE.read_text(encoding="utf-8")
        # Cheap heuristic for a checklist: at least three "[ ]" task markers.
        assert body.count("[ ]") >= 3


class TestSampleTemplate:
    def test_template_has_project_section(self):
        text = TEMPLATE_FILE.read_text(encoding="utf-8")
        assert "[project]" in text
        assert "name = " in text

    def test_template_has_at_least_one_drawing_entry(self):
        text = TEMPLATE_FILE.read_text(encoding="utf-8")
        assert "[[drawings]]" in text

    def test_template_parses_as_toml(self, tmp_path):
        """Confirm the template is actually valid TOML and loadable by our config loader."""
        from autocad_mcp.project import load_project

        # Drop the template into a fresh dir + create placeholder drawing files
        # the loader will resolve. The loader doesn't open them, so empty files
        # are sufficient.
        target = tmp_path / ".acadproj.toml"
        target.write_text(TEMPLATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        # Names must match the template's `[[drawings]] file = "..."` entries.
        for name in ("fp1.dwg", "fp2.dwg", "elev.dxf"):
            (tmp_path / name).write_text("")

        cfg = load_project(tmp_path)
        assert cfg.name == "Example House"
        assert len(cfg.drawings) == 3


# ---------------------------------------------------------------------------
# Subagent discoverability
# ---------------------------------------------------------------------------


class TestSubagent:
    def test_file_exists(self):
        assert SUBAGENT_FILE.exists(), f"Missing {SUBAGENT_FILE}"

    def test_frontmatter_parses(self):
        fm = _parse_frontmatter(SUBAGENT_FILE.read_text(encoding="utf-8"))
        assert fm["name"] == "autocad-architect"
        assert len(fm["description"]) >= 30

    def test_tools_is_list(self):
        fm = _parse_frontmatter(SUBAGENT_FILE.read_text(encoding="utf-8"))
        assert isinstance(fm["tools"], list)
        # Must include the architectural-drafting tools
        assert "arch" in fm["tools"]
        assert "project" in fm["tools"]
        # No P&ID — subagent scope is architectural
        assert "pid" not in fm["tools"]

    def test_name_matches_filename(self):
        fm = _parse_frontmatter(SUBAGENT_FILE.read_text(encoding="utf-8"))
        assert fm["name"] == SUBAGENT_FILE.stem

    def test_body_references_the_skill(self):
        """The subagent should mention the architectural-drafting skill so the
        agent definition + skill stay coupled."""
        body = SUBAGENT_FILE.read_text(encoding="utf-8")
        assert "architectural-drafting" in body
