"""Unit tests for the JSON-object extractor."""
from __future__ import annotations

import json

from agent.providers._parse import extract_json_object, salvage_truncated_json


class TestExtractJsonObject:
    def test_plain_object(self) -> None:
        assert extract_json_object('{"a": 1}') == '{"a": 1}'

    def test_with_surrounding_prose(self) -> None:
        text = 'Sure, here is the action: {"action": "tap", "x": 1, "y": 2}. Done!'
        obj = extract_json_object(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "x": 1, "y": 2}

    def test_markdown_fenced(self) -> None:
        text = '```json\n{"action": "wait", "reason": "loading"}\n```'
        obj = extract_json_object(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "wait", "reason": "loading"}

    def test_nested_braces(self) -> None:
        text = '{"outer": {"inner": {"x": 1}}}'
        assert extract_json_object(text) == text

    def test_brace_inside_string_literal(self) -> None:
        text = '{"text": "this } is in a string", "n": 1}'
        obj = extract_json_object(text)
        assert obj is not None
        assert json.loads(obj) == {"text": "this } is in a string", "n": 1}

    def test_escaped_quote_in_string(self) -> None:
        text = r'{"text": "she said \"hi\"", "n": 1}'
        obj = extract_json_object(text)
        assert obj is not None
        assert json.loads(obj) == {"text": 'she said "hi"', "n": 1}

    def test_no_object_returns_none(self) -> None:
        assert extract_json_object("no braces here") is None

    def test_only_opening_brace_returns_none(self) -> None:
        assert extract_json_object('something { incomplete') is None


class TestSalvageTruncatedJson:
    def test_complete_object_returned_verbatim(self) -> None:
        text = '{"action": "tap", "x": 1, "y": 2}'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "x": 1, "y": 2}

    def test_truncated_at_partial_next_key(self) -> None:
        # The exact failure shape from the user's run: trailing `, "` after
        # the last numeric value, with no closing brace.
        text = '{"action": "tap", "x": 265, "y": 1287, "'
        obj = salvage_truncated_json(text)
        assert obj is not None
        parsed = json.loads(obj)
        assert parsed == {"action": "tap", "x": 265, "y": 1287}

    def test_truncated_at_complete_value_no_trailing_comma(self) -> None:
        text = '{"action": "tap", "x": 265, "y": 1287'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "x": 265, "y": 1287}

    def test_truncated_inside_string_value(self) -> None:
        # The `note` string was opened but never closed. Should fall back to
        # the last complete pair.
        text = '{"action": "tap", "x": 1, "y": 2, "note": "tap the AD'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "x": 1, "y": 2}

    def test_truncated_inside_key(self) -> None:
        text = '{"action": "tap", "x": 1, "y": 2, "no'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "x": 1, "y": 2}

    def test_truncated_at_colon_keeps_previous_pair(self) -> None:
        text = '{"action": "tap", "x": 1, "y": 2, "note":'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "x": 1, "y": 2}

    def test_truncated_inside_nested_object(self) -> None:
        text = '{"action": "tap", "meta": {"a": 1, "b": 2'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "tap", "meta": {"a": 1, "b": 2}}

    def test_no_opening_brace_returns_none(self) -> None:
        assert salvage_truncated_json("garbage with no braces") is None

    def test_truncated_before_first_value_returns_none(self) -> None:
        # No safe boundary was ever reached — nothing to salvage.
        assert salvage_truncated_json('{"action": "ta') is None

    def test_complete_with_surrounding_prose_returns_object(self) -> None:
        text = 'Here you go: {"action": "wait", "reason": "load"} ok'
        obj = salvage_truncated_json(text)
        assert obj is not None
        assert json.loads(obj) == {"action": "wait", "reason": "load"}

    def test_escaped_quote_inside_string_does_not_confuse_state(self) -> None:
        text = r'{"action": "type", "text": "she said \"hi\"'
        obj = salvage_truncated_json(text)
        assert obj is not None
        # The text value was never closed, but action was.
        assert json.loads(obj) == {"action": "type"}
