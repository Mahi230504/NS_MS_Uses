"""Whitelist safe actions, block dangerous ones."""
from __future__ import annotations

import json


ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"tap", "type", "swipe", "done", "need_approval", "wait"}
)

BLOCKED_ACTIONS: tuple[str, ...] = (
    "call",
    "sms",
    "factory_reset",
    "uninstall",
    "adb shell rm",
)

# User/agent content fields are excluded from the blocklist substring scan so
# that legitimate text input like "call mom" or "remove from cart" is not
# false-positive blocked. Structural fields are still scanned.
_CONTENT_FIELDS = frozenset({"text", "reason", "summary"})


def validate(action_json: dict) -> tuple[bool, str]:
    """Return (ok, reason).

    Hard rule: action type must be in ALLOWED_ACTIONS.
    Defense in depth: structural fields (action type and any unknown keys) are
    scanned for BLOCKED_ACTIONS terms so the agent cannot smuggle them through
    a custom action type or non-content field.
    """
    if not isinstance(action_json, dict):
        return False, f"action must be a dict, got {type(action_json).__name__}"

    action_type = action_json.get("action")
    if not action_type:
        return False, "missing 'action' field"

    if action_type not in ALLOWED_ACTIONS:
        return False, f"action '{action_type}' is not in the allowed set"

    structural = {k: v for k, v in action_json.items() if k not in _CONTENT_FIELDS}
    blob = json.dumps(structural, default=str).lower()
    for term in BLOCKED_ACTIONS:
        if term in blob:
            return False, f"blocked term '{term}' present in action payload"

    return True, "ok"
