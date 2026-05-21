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
