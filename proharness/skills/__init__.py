"""Skills: discoverable, frontmatter-described capabilities.

A skill is a directory with a SKILL.md file containing YAML frontmatter
(name, description, trigger keywords) + a markdown body of instructions the
agent loads when the skill is relevant.

This mirrors the editor's skill convention so skills are portable. Skills are
distinct from tools: a tool is a callable function; a skill is a body of
instructions + tool usage guidance that the agent reads to perform a class of
task.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)


@dataclass
class Skill:
    name: str
    description: str
    trigger_keywords: list[str] = field(default_factory=list)
    body: str = ""
    path: str = ""

    def match_text(self) -> str:
        """Text used for skill selection (embedded by the tool selector /
        skill router)."""
        return f"{self.name}: {self.description} triggers={self.trigger_keywords}"

    def load(self) -> str:
        """Return the skill body for injection into agent context."""
        if not self.body and self.path:
            self.body = Path(self.path).read_text(encoding="utf-8")
            # Strip the frontmatter from the body.
            m = _FRONTMATTER_RE.match(self.body)
            if m:
                self.body = m.group(2).strip()
        return self.body


def parse_skill_md(path: str | Path) -> Skill | None:
    """Parse a SKILL.md file. Returns None if frontmatter is missing/malformed."""
    import yaml

    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    body = m.group(2).strip()
    name = meta.get("name") or p.parent.name
    desc = meta.get("description") or ""
    triggers = meta.get("trigger") or meta.get("triggers") or meta.get("keywords") or []
    if isinstance(triggers, str):
        triggers = [triggers]
    return Skill(
        name=str(name),
        description=str(desc),
        trigger_keywords=[str(t) for t in triggers],
        body=body,
        path=str(p),
    )


def discover_skills(*dirs: str | Path) -> list[Skill]:
    """Find SKILL.md files under the given directories.

    A skill directory is any directory containing a SKILL.md file. Skill
    directories may live one level deep (dir/<name>/SKILL.md).
    """
    skills: list[Skill] = []
    seen_names: set[str] = set()
    for d in dirs:
        dpath = Path(d).expanduser()
        if not dpath.exists():
            continue
        # Direct skill.md under d
        for candidate in sorted(dpath.glob("**/SKILL.md")):
            skill = parse_skill_md(candidate)
            if skill and skill.name not in seen_names:
                skills.append(skill)
                seen_names.add(skill.name)
    return skills


def default_skill_dirs() -> list[str]:
    """Built-in examples + user skills dir."""
    import os

    here = Path(__file__).parent / "examples"
    user_dir = Path(os.path.expanduser("~/.proharness/skills"))
    return [str(here), str(user_dir)]
