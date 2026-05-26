"""Provider-response parsing helpers."""
from __future__ import annotations


def extract_json_object(text: str) -> str | None:
    """Return the first top-level {...} object in text, or None.

    Handles surrounding prose, markdown fences, and nested braces (including
    braces inside string literals).
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def salvage_truncated_json(text: str) -> str | None:
    """Best-effort closer for a JSON object that was cut off by token limit.

    Walks the input, recording every safe truncation boundary — the index
    just before a comma or just after a complete value — along with the
    closing characters needed at that point. If the input is complete,
    returns the full object. If it was truncated, returns the longest
    parseable prefix with the necessary `}`/`]` appended. Returns None
    when no opening brace exists or no safe boundary was ever reached
    (e.g. text was cut off inside the very first key).

    The common Gemini-via-OpenRouter failure mode looks like:
        '{"action": "tap", "x": 265, "y": 1287, "'
    which this collapses to:
        '{"action": "tap", "x": 265, "y": 1287}'
    losing only the unstarted `note` field rather than the whole step.
    """
    start = text.find("{")
    if start == -1:
        return None

    stack: list[str] = []  # closers needed: '}' or ']'
    in_string = False
    escape = False
    safe_end: int | None = None
    safe_closers: str = ""
    # A "value just finished" marker lets us treat the position right
    # after a string/number/closing-brace as a valid truncation point,
    # not just after commas. Helps when the model is cut off between
    # `"y": 1287` and the trailing `,` or `}`.
    value_just_closed = False
    value_end_index = -1

    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if c == "\\" and in_string:
            escape = True
            i += 1
            continue
        if c == '"':
            if in_string:
                in_string = False
                value_just_closed = True
                value_end_index = i + 1
            else:
                in_string = True
                value_just_closed = False
            i += 1
            continue
        if in_string:
            i += 1
            continue

        if c == "{":
            stack.append("}")
            value_just_closed = False
        elif c == "[":
            stack.append("]")
            value_just_closed = False
        elif c == "}" or c == "]":
            if stack:
                stack.pop()
            value_just_closed = True
            value_end_index = i + 1
            if not stack:
                # Returned to depth 0 — the object is complete.
                return text[start : i + 1]
        elif c == ",":
            # Truncating right before this comma is always safe: the
            # element just before it was complete.
            safe_end = i
            safe_closers = "".join(reversed(stack))
            value_just_closed = False
        elif c == ":":
            value_just_closed = False
        elif not c.isspace():
            # A non-space, non-structural char is part of a number / literal
            # (true/false/null). Mark the position after each such char as
            # a tentative value-end; the next iteration overwrites until
            # the value finishes.
            value_end_index = i + 1
            value_just_closed = True
        i += 1

    # EOF without depth returning to 0 — truncation occurred.
    if value_just_closed and value_end_index > start:
        # A complete value sits at value_end_index; close any open
        # containers above it.
        candidate_end = value_end_index
        candidate_closers = "".join(reversed(stack))
        if safe_end is None or candidate_end > safe_end:
            return text[start:candidate_end] + candidate_closers

    if safe_end is None:
        return None
    return text[start:safe_end] + safe_closers
