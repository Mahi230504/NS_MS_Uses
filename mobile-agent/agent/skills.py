"""App-specific guidance loaded from on-disk markdown.

A skill is a short markdown file at `<skills_dir>/<package_name>.md` (e.g.
`skills/com.blinkit.markets.md`). When the foreground app matches the
filename's stem, the file's contents are prepended to the vision prompt.

Keep skills small — every loop iteration sends them as input tokens.
"""
from __future__ import annotations

from pathlib import Path


class SkillRegistry:
    """Cached, lazy-loaded map of {package_name: markdown_text}."""

    def __init__(self, skills_dir: Path) -> None:
        self._dir = skills_dir
        self._cache: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if not self._dir.is_dir():
            return {}
        skills: dict[str, str] = {}
        for path in self._dir.glob("*.md"):
            # README and other meta-docs use uppercase or no dots — skip them
            # so they're not picked up as package skills.
            if path.stem.lower() == "readme" or "." not in path.stem:
                continue
            try:
                skills[path.stem] = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
        return skills

    def get(self, package_name: str | None) -> str | None:
        if not package_name:
            return None
        if self._cache is None:
            self._cache = self._load()
        return self._cache.get(package_name)

    def list_packages(self) -> list[str]:
        if self._cache is None:
            self._cache = self._load()
        return sorted(self._cache)
