"""Parse Android `uiautomator dump` XML into a model-friendly text listing.

The raw XML from uiautomator can have hundreds of nodes — most of them
invisible layout wrappers. The vision model only needs *interactable* and
*labelled* elements with their on-screen bounds so it can pick accurate
tap coordinates instead of guessing pixels.

Output shape (compact, sorted top-to-bottom then left-to-right):

    UI elements (clickable + text-visible, center coords):
    - "Search" at (540, 200), id=com.foo:id/search, class=EditText
    - "Add"    at (920, 760), class=Button
    - "Cart"   at (60, 2330),  desc=cart, class=ImageView
    ...

If you want to tap an element, use its center coordinates exactly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET


_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
# Short-class map: full Android class names are noisy, the suffix is enough.
_CLASS_SHORT = {
    "android.widget.EditText": "EditText",
    "android.widget.Button": "Button",
    "android.widget.ImageButton": "ImageButton",
    "android.widget.ImageView": "ImageView",
    "android.widget.TextView": "TextView",
    "android.widget.CheckBox": "CheckBox",
    "android.widget.RadioButton": "RadioButton",
    "android.widget.Switch": "Switch",
    "android.view.ViewGroup": "ViewGroup",
    "android.webkit.WebView": "WebView",
    "androidx.recyclerview.widget.RecyclerView": "RecyclerView",
    "androidx.viewpager.widget.ViewPager": "ViewPager",
}
# Cap so we don't blow up the prompt on heavy screens.
_MAX_ELEMENTS = 60


@dataclass(frozen=True)
class UiElement:
    text: str
    desc: str          # content-desc
    resource_id: str
    class_name: str
    cx: int            # center x
    cy: int            # center y
    bounds: tuple[int, int, int, int]
    clickable: bool

    @property
    def has_label(self) -> bool:
        return bool(self.text or self.desc or self.resource_id)


def parse(xml: str) -> list[UiElement]:
    """Parse uiautomator XML → flat list of interactable/labelled elements."""
    if not xml:
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []

    elements: list[UiElement] = []
    for node in root.iter("node"):
        attrs = node.attrib
        bounds = _parse_bounds(attrs.get("bounds", ""))
        if bounds is None:
            continue
        x1, y1, x2, y2 = bounds
        if x2 <= x1 or y2 <= y1:
            continue  # zero-area, skip
        clickable = attrs.get("clickable", "false") == "true"
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        rid = (attrs.get("resource-id") or "").strip()
        cls = (attrs.get("class") or "").strip()
        # Keep elements that are either clickable OR carry a useful label.
        if not (clickable or text or desc or rid):
            continue
        elements.append(
            UiElement(
                text=text,
                desc=desc,
                resource_id=rid,
                class_name=cls,
                cx=(x1 + x2) // 2,
                cy=(y1 + y2) // 2,
                bounds=bounds,
                clickable=clickable,
            )
        )
    return elements


def to_prompt_section(xml: str) -> str:
    """Render the parsed tree as a short, model-readable text block.

    Empty string when the tree is empty or unparseable — caller can just
    interpolate it into the prompt without conditionals.
    """
    elements = parse(xml)
    if not elements:
        return ""
    # Sort reading-order so the listing matches what the model sees visually.
    elements.sort(key=lambda e: (e.cy, e.cx))
    lines: list[str] = []
    for e in elements[:_MAX_ELEMENTS]:
        label = _format_label(e)
        cls_short = _short_class(e.class_name)
        attrs: list[str] = []
        if e.resource_id:
            # Trim package prefix for readability: com.foo:id/bar → bar
            short_id = e.resource_id.split("/")[-1] if "/" in e.resource_id else e.resource_id
            attrs.append(f"id={short_id}")
        if cls_short:
            attrs.append(f"class={cls_short}")
        if e.clickable:
            attrs.append("clickable")
        attr_str = ", ".join(attrs)
        lines.append(f"- {label} at ({e.cx}, {e.cy}){'  ' + attr_str if attr_str else ''}")
    suffix = ""
    if len(elements) > _MAX_ELEMENTS:
        suffix = f"\n  ... and {len(elements) - _MAX_ELEMENTS} more (truncated)"
    return (
        "UI elements (from accessibility tree, center coords are tap targets):\n"
        + "\n".join(lines)
        + suffix
    )


def _parse_bounds(s: str) -> tuple[int, int, int, int] | None:
    m = _BOUNDS_RE.search(s)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _format_label(e: UiElement) -> str:
    """Best human-readable label, preferring text > desc > id."""
    if e.text:
        return f'"{_truncate(e.text)}"'
    if e.desc:
        return f"desc={_truncate(e.desc)}"
    if e.resource_id:
        short_id = e.resource_id.split("/")[-1] if "/" in e.resource_id else e.resource_id
        return f"id={short_id}"
    return _short_class(e.class_name) or "(unlabelled)"


def _short_class(cls: str) -> str:
    if cls in _CLASS_SHORT:
        return _CLASS_SHORT[cls]
    if "." in cls:
        return cls.rsplit(".", 1)[-1]
    return cls


def _truncate(s: str, n: int = 40) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"
