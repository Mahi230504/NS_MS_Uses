"""Unit tests for the JSON-object extractor."""
from __future__ import annotations

import json

from agent.providers._parse import extract_json_object


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
