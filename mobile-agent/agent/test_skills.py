"""Unit tests for SkillRegistry."""
from __future__ import annotations

from pathlib import Path

from agent.skills import SkillRegistry


class TestSkillRegistry:
    def test_returns_none_for_missing_dir(self, tmp_path: Path) -> None:
        reg = SkillRegistry(tmp_path / "does-not-exist")
        assert reg.get("com.example.app") is None

    def test_loads_package_skill(self, tmp_path: Path) -> None:
        (tmp_path / "com.example.app.md").write_text("hello skill")
        reg = SkillRegistry(tmp_path)
        assert reg.get("com.example.app") == "hello skill"

    def test_ignores_readme(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("this is meta")
        (tmp_path / "com.example.app.md").write_text("real skill")
        reg = SkillRegistry(tmp_path)
        # README has no dot in stem so it's also filtered, but make the rule
        # explicit:
        assert reg.list_packages() == ["com.example.app"]

    def test_ignores_files_without_dotted_stem(self, tmp_path: Path) -> None:
        (tmp_path / "notes.md").write_text("loose notes")
        (tmp_path / "com.x.app.md").write_text("ok")
        reg = SkillRegistry(tmp_path)
        assert reg.list_packages() == ["com.x.app"]

    def test_unknown_package_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "com.example.app.md").write_text("x")
        reg = SkillRegistry(tmp_path)
        assert reg.get("com.other.app") is None
        assert reg.get(None) is None

    def test_caches_after_first_load(self, tmp_path: Path) -> None:
        (tmp_path / "com.a.b.md").write_text("first")
        reg = SkillRegistry(tmp_path)
        assert reg.get("com.a.b") == "first"
        # Mutate file on disk; cached value should remain.
        (tmp_path / "com.a.b.md").write_text("second")
        assert reg.get("com.a.b") == "first"
