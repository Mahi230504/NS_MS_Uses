"""Sanity tests for the app/task registry."""
from __future__ import annotations

from pathlib import Path

import pytest

from bot.apps import APPS, get_app, get_task, render_prompt


SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


class TestRegistry:
    def test_at_least_twenty_apps(self) -> None:
        assert len(APPS) >= 20

    def test_app_ids_unique(self) -> None:
        ids = [a.id for a in APPS]
        assert len(ids) == len(set(ids))

    def test_task_ids_unique_within_each_app(self) -> None:
        for app in APPS:
            ids = [t.id for t in app.tasks]
            assert len(ids) == len(set(ids)), f"duplicate task id in {app.id}"

    def test_every_app_has_at_least_one_task(self) -> None:
        for app in APPS:
            assert app.tasks, f"{app.id} has no tasks"

    def test_every_app_has_free_form_escape(self) -> None:
        for app in APPS:
            ids = [t.id for t in app.tasks]
            assert "free" in ids, f"{app.id} missing 'free' (Type my own)"

    def test_every_app_has_a_skill_file(self) -> None:
        missing: list[str] = []
        for app in APPS:
            path = SKILLS_DIR / f"{app.package}.md"
            if not path.exists():
                missing.append(app.package)
        assert not missing, f"missing skill files: {missing}"

    def test_param_tasks_have_param_placeholder(self) -> None:
        # If a task asks the user for a param, the template should actually
        # consume it — otherwise the param goes nowhere.
        for app in APPS:
            for t in app.tasks:
                if t.needs_param:
                    assert "{param}" in t.template, (
                        f"{app.id}/{t.id} prompts for a param but template "
                        f"doesn't reference {{param}}"
                    )

    def test_package_names_are_dotted(self) -> None:
        for app in APPS:
            assert "." in app.package, f"{app.id} package looks wrong: {app.package!r}"


class TestLookup:
    def test_get_app_known(self) -> None:
        assert get_app("blinkit") is not None

    def test_get_app_unknown(self) -> None:
        assert get_app("nosuchapp") is None

    def test_get_task_known(self) -> None:
        app = get_app("blinkit")
        assert get_task(app, "order") is not None

    def test_get_task_unknown(self) -> None:
        app = get_app("blinkit")
        assert get_task(app, "nosuch") is None


class TestRenderPrompt:
    def test_substitutes_param(self) -> None:
        assert render_prompt("buy {param}", "milk") == "buy milk"

    def test_no_param_yields_unchanged(self) -> None:
        assert render_prompt("open inbox", None) == "open inbox"

    def test_template_without_placeholder_is_tolerated(self) -> None:
        # We pass a param to a template that doesn't include {param} — should
        # not raise, just return the template as-is.
        assert render_prompt("open inbox", "ignored") == "open inbox"

    def test_param_is_stripped(self) -> None:
        assert render_prompt("search for {param}", "  milk  ") == "search for milk"
