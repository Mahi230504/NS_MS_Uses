"""Whitelist safe actions, block dangerous ones."""
from __future__ import annotations

import json


ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {"tap", "type", "swipe", "done", "need_approval", "wait", "report"}
)

# Common synonyms from different model families. Qwen / GUI-agent models
# typically emit "click" instead of "tap"; some emit "input"/"input_text"
# instead of "type". Normalising at the validator means the action_executor
# only ever sees the canonical names.
_ACTION_ALIASES: dict[str, str] = {
    "click": "tap",
    "press": "tap",
    "input": "type",
    "input_text": "type",
    "scroll": "swipe",
}

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
#   - text:    the literal characters being typed
#   - reason:  the model's narration for wait/need_approval
#   - summary: the final summary on done
#   - note:    short human-readable description on state-changing actions
#              (e.g. "tapping ADD on Amul Taaza Milk 500ml")
#   - data:    structured model-generated payload on a `report` terminal action
#              (price/eta/item_name/notes). It's never dispatched to the device,
#              so a product name like "remove-bee balm" must not false-trip the
#              blocklist substring scan.
_CONTENT_FIELDS = frozenset({"text", "reason", "summary", "note", "data"})


_TAP_POINT_KEYS = ("coordinate", "point", "coord", "position", "bbox_2d", "box_2d")
_SWIPE_START_KEYS = ("start", "from", "coordinate", "p1", "start_point")
_SWIPE_END_KEYS = ("end", "to", "coordinate2", "p2", "end_point")


def _as_int(value) -> int | None:
    """Best-effort coercion to int. Strings, floats, and numeric-strings work;
    lists, dicts, and None return None so the caller can fall back."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            return int(float(s))
        except ValueError:
            return None
    return None


def _as_point(value) -> tuple[int, int] | None:
    """Coerce common point shapes to (x, y).

    Accepts:
      [x, y]                — flat 2-list / tuple
      [[x, y]]              — wrapped (Qwen sometimes returns box_2d this way)
      [x1, y1, x2, y2]      — bbox; returns the center
      {"x": x, "y": y}      — dict
    """
    if isinstance(value, (list, tuple)):
        if len(value) == 1 and isinstance(value[0], (list, tuple)):
            return _as_point(value[0])
        if len(value) == 2:
            x, y = _as_int(value[0]), _as_int(value[1])
            return (x, y) if x is not None and y is not None else None
        if len(value) == 4:
            xs = [_as_int(v) for v in value]
            if all(v is not None for v in xs):
                x1, y1, x2, y2 = xs  # type: ignore[misc]
                return ((x1 + x2) // 2, (y1 + y2) // 2)
            return None
    if isinstance(value, dict):
        x, y = _as_int(value.get("x")), _as_int(value.get("y"))
        return (x, y) if x is not None and y is not None else None
    return None


def _first_point(action: dict, keys: tuple[str, ...]) -> tuple[int, int] | None:
    for k in keys:
        if k in action:
            pt = _as_point(action[k])
            if pt is not None:
                return pt
    return None


def _normalize_coords(action_type: str, action: dict) -> None:
    """Rewrite Qwen-style nested coordinate fields to our canonical x/y form.

    Mutates `action` in place. Recognises every Qwen 2.5 VL / GUI-agent shape
    we've seen: nested lists, bboxes (treated as center), dict points, string
    ints. Anything still unparseable is left alone — `validate()` will reject
    it downstream rather than crashing the executor.
    """
    if action_type == "tap":
        # First, look for a packed point under any of the known keys.
        pt = _first_point(action, _TAP_POINT_KEYS)
        # Fallback: someone packed both coords into "x" (and possibly "y" too).
        if pt is None:
            pt = _as_point(action.get("x"))
        if pt is not None:
            action["x"], action["y"] = pt
            return
        # Last resort: x and y are present but in a non-canonical scalar form
        # (e.g. strings, floats). Coerce in place so the executor sees ints.
        x = _as_int(action.get("x"))
        y = _as_int(action.get("y"))
        if x is not None:
            action["x"] = x
        if y is not None:
            action["y"] = y

    elif action_type == "swipe":
        start = _first_point(action, _SWIPE_START_KEYS)
        end = _first_point(action, _SWIPE_END_KEYS)
        if start is None and isinstance(action.get("x1"), (list, tuple)):
            start = _as_point(action.get("x1"))
        if end is None and isinstance(action.get("x2"), (list, tuple)):
            end = _as_point(action.get("x2"))
        if start is not None:
            action["x1"], action["y1"] = start
        if end is not None:
            action["x2"], action["y2"] = end
        for k in ("x1", "y1", "x2", "y2"):
            v = action.get(k)
            if isinstance(v, (int, float)):
                continue
            coerced = _as_int(v)
            if coerced is not None:
                action[k] = coerced


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

    # Normalise model-family synonyms before validation, in place. Downstream
    # (action_executor, HITL, persistence) only sees canonical names.
    if action_type in _ACTION_ALIASES:
        action_type = _ACTION_ALIASES[action_type]
        action_json["action"] = action_type

    if action_type not in ALLOWED_ACTIONS:
        return False, f"action '{action_type}' is not in the allowed set"

    # Coordinate-shape normalisation. Qwen and similar GUI-agent models
    # often emit `"coordinate": [x, y]` instead of separate `"x"` / `"y"`
    # integers, or even `"start": [x1,y1], "end": [x2,y2]` for swipes.
    # Rewrite to our canonical x/y form so downstream code stays simple.
    _normalize_coords(action_type, action_json)

    # Hard guard: after normalisation, tap/swipe MUST have scalar int coords.
    # If a list / dict / None slipped through, the executor's int() would
    # crash with a noisy TypeError — reject here with a clear reason instead.
    if action_type == "tap":
        for k in ("x", "y"):
            if not isinstance(action_json.get(k), (int, float)):
                return False, (
                    f"tap missing/invalid '{k}' after normalisation "
                    f"(got {type(action_json.get(k)).__name__}); "
                    "expected integer pixel coordinate"
                )
    elif action_type == "swipe":
        for k in ("x1", "y1", "x2", "y2"):
            if not isinstance(action_json.get(k), (int, float)):
                return False, (
                    f"swipe missing/invalid '{k}' after normalisation "
                    f"(got {type(action_json.get(k)).__name__}); "
                    "expected integer pixel coordinate"
                )
    elif action_type == "report":
        # Read-only probe terminal. `data` carries the structured quote; if the
        # model includes it, it must be a JSON object. A missing `data` is
        # tolerated — the orchestrator coerces it to {} — so a probe that found
        # nothing can still report cleanly.
        data = action_json.get("data")
        if data is not None and not isinstance(data, dict):
            return False, (
                f"report 'data' must be an object, got {type(data).__name__}"
            )

    structural = {k: v for k, v in action_json.items() if k not in _CONTENT_FIELDS}
    blob = json.dumps(structural, default=str).lower()
    for term in BLOCKED_ACTIONS:
        if term in blob:
            return False, f"blocked term '{term}' present in action payload"

    return True, "ok"
