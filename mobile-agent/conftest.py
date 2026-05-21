"""Shared pytest fixtures and live-test gating."""
from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("MOBILE_AGENT_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(reason="needs MOBILE_AGENT_LIVE=1 (real adb/emulator)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
