"""Unit tests for the action validator."""
from __future__ import annotations

from security.action_validator import ALLOWED_ACTIONS, validate


class TestValidate:
    def test_tap_is_allowed(self) -> None:
        ok, reason = validate({"action": "tap", "x": 100, "y": 200})
        assert ok, reason

    def test_done_is_allowed(self) -> None:
        ok, _ = validate({"action": "done", "summary": "all good"})
        assert ok

    def test_need_approval_is_allowed(self) -> None:
        ok, _ = validate({"action": "need_approval", "reason": "OTP entry"})
        assert ok

    def test_unknown_action_is_rejected(self) -> None:
        ok, reason = validate({"action": "screenshot"})
        assert not ok
        assert "not in the allowed set" in reason

    def test_missing_action_field(self) -> None:
        ok, reason = validate({"x": 1, "y": 2})
        assert not ok
        assert "missing 'action'" in reason

    def test_non_dict_payload(self) -> None:
        ok, reason = validate("tap")  # type: ignore[arg-type]
        assert not ok
        assert "must be a dict" in reason

    # Regression: legitimate text input "call mom" must not be blocked.
    def test_type_call_mom_is_not_blocked(self) -> None:
        ok, reason = validate({"action": "type", "text": "call mom"})
        assert ok, reason

    def test_done_summary_with_call_is_not_blocked(self) -> None:
        ok, _ = validate(
            {"action": "done", "summary": "added a call to action button"}
        )
        assert ok

    def test_need_approval_reason_with_uninstall_is_not_blocked(self) -> None:
        # need_approval is the EXIT path for sensitive operations — its reason
        # field will frequently mention dangerous words. Must remain allowed.
        ok, _ = validate(
            {"action": "need_approval", "reason": "user is about to uninstall app"}
        )
        assert ok

    # Defense in depth: structural fields are still scanned.
    def test_structural_field_with_blocked_term(self) -> None:
        ok, reason = validate({"action": "tap", "x": 1, "y": 2, "intent": "call"})
        assert not ok
        assert "blocked term" in reason

    def test_note_field_is_exempt_from_blocklist(self) -> None:
        # `note` is human-readable narration ("tap CALL on the contact") and
        # must NOT be scanned for blocked terms like "call". Otherwise every
        # ADD on Blinkit gets blocked because the note describes the product.
        ok, reason = validate(
            {"action": "tap", "x": 1, "y": 2, "note": "tap CALL on the contact card"}
        )
        assert ok, reason

    def test_click_normalized_to_tap(self) -> None:
        # Qwen and other GUI-agent models emit "click" instead of "tap".
        # The validator normalises this in place.
        action = {"action": "click", "x": 100, "y": 200}
        ok, _ = validate(action)
        assert ok
        assert action["action"] == "tap"  # mutated to canonical name

    def test_input_normalized_to_type(self) -> None:
        action = {"action": "input", "text": "hello"}
        ok, _ = validate(action)
        assert ok
        assert action["action"] == "type"

    def test_scroll_normalized_to_swipe(self) -> None:
        action = {"action": "scroll", "x1": 1, "y1": 2, "x2": 3, "y2": 4}
        ok, _ = validate(action)
        assert ok
        assert action["action"] == "swipe"

    def test_qwen_coordinate_list_normalized_to_x_y(self) -> None:
        # Qwen-style: {"action": "click", "coordinate": [x, y]}
        action = {"action": "click", "coordinate": [300, 400]}
        ok, _ = validate(action)
        assert ok
        assert action["action"] == "tap"
        assert action["x"] == 300
        assert action["y"] == 400

    def test_swipe_start_end_lists_normalized(self) -> None:
        action = {
            "action": "swipe",
            "start": [100, 200],
            "end": [300, 400],
            "duration_ms": 200,
        }
        ok, _ = validate(action)
        assert ok
        assert action["x1"] == 100 and action["y1"] == 200
        assert action["x2"] == 300 and action["y2"] == 400

    def test_allowed_action_set_is_locked_down(self) -> None:
        # The allowed set is small and intentional; this test fails if anyone
        # widens it without thinking. `report` is the read-only probe terminal
        # used by cross-app comparison (never dispatched to the device).
        assert ALLOWED_ACTIONS == frozenset(
            {"tap", "type", "swipe", "done", "need_approval", "wait", "report"}
        )

    # ------------------------------------------------------------------
    # Qwen 2.5 VL coord-shape regression tests. Each test covers a real or
    # plausible shape we've seen in audit logs; the validator must either
    # normalise to scalar int x/y or reject cleanly — never let a list
    # reach the executor's `int(action["x"])` and crash with TypeError.

    def test_x_as_packed_list_normalized(self) -> None:
        # Real-world failure 2026-05-25: Qwen returned both coords inside x.
        action = {"action": "tap", "x": [517, 457], "note": "tap search bar"}
        ok, reason = validate(action)
        assert ok, reason
        assert action["x"] == 517 and action["y"] == 457

    def test_point_key_normalized(self) -> None:
        action = {"action": "tap", "point": [100, 200]}
        ok, _ = validate(action)
        assert ok
        assert action["x"] == 100 and action["y"] == 200

    def test_position_key_normalized(self) -> None:
        action = {"action": "tap", "position": [50, 60]}
        ok, _ = validate(action)
        assert ok
        assert action["x"] == 50 and action["y"] == 60

    def test_bbox_4_normalized_to_center(self) -> None:
        # Qwen sometimes emits a 4-element bbox; we tap the center.
        action = {"action": "tap", "bbox_2d": [100, 200, 300, 400]}
        ok, _ = validate(action)
        assert ok
        assert action["x"] == 200 and action["y"] == 300

    def test_box_2d_nested_list_normalized(self) -> None:
        # [[x, y]] wrapped form — Qwen GUI mode does this occasionally.
        action = {"action": "tap", "box_2d": [[150, 250]]}
        ok, _ = validate(action)
        assert ok
        assert action["x"] == 150 and action["y"] == 250

    def test_string_coords_coerced_to_int(self) -> None:
        action = {"action": "tap", "x": "100", "y": "200"}
        ok, _ = validate(action)
        assert ok
        assert action["x"] == 100 and action["y"] == 200

    def test_float_coords_coerced_to_int(self) -> None:
        action = {"action": "tap", "x": 100.7, "y": 200.3}
        ok, _ = validate(action)
        assert ok
        # floats are accepted as-is (executor int-casts); we only care no crash.

    def test_dict_point_normalized(self) -> None:
        action = {"action": "tap", "coordinate": {"x": 11, "y": 22}}
        ok, _ = validate(action)
        assert ok
        assert action["x"] == 11 and action["y"] == 22

    def test_tap_with_garbage_x_is_rejected_not_crashed(self) -> None:
        # Defensive: if normalisation can't extract scalar coords, reject
        # with a clear reason instead of letting executor crash on int().
        action = {"action": "tap", "x": {"weird": "shape"}, "y": None}
        ok, reason = validate(action)
        assert not ok
        assert "x" in reason or "y" in reason
        assert "expected integer" in reason

    def test_tap_with_single_element_x_list_is_rejected(self) -> None:
        # A 1-element list isn't a valid point; reject rather than guess.
        action = {"action": "tap", "x": [517], "y": [457]}
        ok, reason = validate(action)
        assert not ok
        assert "expected integer" in reason

    def test_swipe_coordinate_pair_normalized(self) -> None:
        action = {
            "action": "swipe",
            "coordinate": [10, 20],
            "coordinate2": [30, 40],
        }
        ok, _ = validate(action)
        assert ok
        assert action["x1"] == 10 and action["y1"] == 20
        assert action["x2"] == 30 and action["y2"] == 40

    def test_swipe_with_x1_as_packed_list(self) -> None:
        # Same trick as tap, applied to swipe.
        action = {
            "action": "swipe", "x1": [10, 20], "x2": [30, 40],
        }
        ok, _ = validate(action)
        assert ok
        assert action["x1"] == 10 and action["y1"] == 20
        assert action["x2"] == 30 and action["y2"] == 40
