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
# Tokens that mark an element as a primary action target (ADD button, qty
# stepper, etc.) — surfaced with an [ACTION] prefix so the model picks them
# instead of the surrounding card.
_ACTION_TEXT_TOKENS = {"add", "+", "−", "-", "buy", "place order", "pay",
                       "checkout", "proceed", "place"}
_ACTION_ID_TOKENS = ("add_to_cart", "add_btn", "add_button", "btn_add",
                     "plus", "minus", "increment", "decrement", "qty_inc",
                     "qty_dec", "stepper", "checkout", "place_order",
                     "proceed", "pay_button")
_ACTION_DESC_TOKENS = ("add to cart", "increase quantity", "decrease quantity",
                       "checkout", "place order")

# Patterns that strongly suggest "this is a category/landing tile, not a
# product card you can add to cart". Used by the [CATEGORY?] annotation to
# steer the model away. None of these are 100% conclusive on their own —
# the absence-of-nearby-ADD check (see `_row_has_action_button`) is the
# primary signal; these add precision when an ADD button is technically
# present elsewhere on screen (e.g. cart header) but not for this row.
_CATEGORY_ID_TOKENS = ("category", "banner", "tile", "collection",
                       "browse", "showcase", "promo", "carousel",
                       "merchandise", "rail")
# Substrings in card text that pattern-match Blinkit-style category names.
# Real product titles describe a single SKU ("Maggi 2-Minute Masala Noodles
# 70g") and rarely contain these tokens.
_CATEGORY_TEXT_PATTERNS = (
    " n ",          # "Maggi N Maggi House" / "Bread N Eggs"
    " range",
    " store",
    " house",
    " shop",
    "shop by",
    "explore",
    "browse",
    "view all",
    "see all",
    "categories",
)
# Horizontal-band tolerance for "same row" matching when checking whether a
# card has an [ACTION] sibling. Product cards on Blinkit are ~600px tall;
# 250px catches the price/ADD strip below the title without bleeding into
# the next card.
_SAME_ROW_TOLERANCE_PX = 250

# Y-coordinate ceiling for "top of screen" header detection. Anything above
# this is the device's address/location header on most Indian grocery apps
# (Blinkit, Zepto, Instamart). Real search bars sit BELOW this line on a
# 1080x2400 screen. Used by the [LOCATION] marker.
_TOP_HEADER_Y_MAX = 300
# Substring tokens (case-insensitive) that mark a clickable top-of-screen
# element as the delivery/location header. Used to add a [LOCATION] prefix
# so the model doesn't mistake it for the search bar.
_LOCATION_TOKENS = (
    "deliver to",
    "deliver in",
    "delivery in",
    "delivery to",
    "delivering to",
    "delivery address",
    "delivery location",
    "your location",
    "set location",
    "change location",
    "change address",
    "select address",
    "select location",
    "home address",
    "current location",
)
# Resource-id tokens for the same purpose. App-internal IDs are the most
# reliable signal — even when the visible text is just an address.
_LOCATION_ID_TOKENS = (
    "location_header",
    "address_header",
    "delivery_header",
    "deliver_header",
    "location_bar",
    "address_bar",
    "header_address",
    "header_location",
)


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

    @property
    def is_action(self) -> bool:
        """True when this element looks like a primary action target.

        Used to surface ADD / qty-stepper / checkout buttons with an
        [ACTION] marker in the prompt so the model doesn't tap the
        surrounding card instead.
        """
        t = self.text.strip().lower()
        if t in _ACTION_TEXT_TOKENS:
            return True
        rid = self.resource_id.lower()
        if any(tok in rid for tok in _ACTION_ID_TOKENS):
            return True
        d = self.desc.strip().lower()
        if any(tok in d for tok in _ACTION_DESC_TOKENS):
            return True
        return False

    @property
    def looks_like_location_header(self) -> bool:
        """True if this element looks like a delivery-location header.

        Used by `_prompt_prefix` to add a `[LOCATION]` marker so the model
        doesn't mistake the top-of-screen address bar for the search bar.
        Two-signal heuristic: clickable + in the top y-band + (text token
        match OR resource-id token match).
        """
        if not self.clickable:
            return False
        if self.cy > _TOP_HEADER_Y_MAX:
            return False
        rid = self.resource_id.lower()
        if any(tok in rid for tok in _LOCATION_ID_TOKENS):
            return True
        haystack = f" {self.text.lower()} {self.desc.lower()} "
        if any(tok in haystack for tok in _LOCATION_TOKENS):
            return True
        return False

    @property
    def looks_like_category_text(self) -> bool:
        """True if this element's text/desc/id matches a category-tile pattern.

        Used together with the no-nearby-ADD-button check to mark cards as
        `[CATEGORY?]` in the prompt so the model doesn't tap a brand-styled
        category banner thinking it's a product. See `_CATEGORY_*` constants
        for the specific patterns recognised.
        """
        rid = self.resource_id.lower()
        if any(tok in rid for tok in _CATEGORY_ID_TOKENS):
            return True
        haystack = f" {self.text.lower()} {self.desc.lower()} "
        if any(pat in haystack for pat in _CATEGORY_TEXT_PATTERNS):
            return True
        return False

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bounds
        return max(0, x2 - x1) * max(0, y2 - y1)


def parse(xml: str) -> list[UiElement]:
    """Parse uiautomator XML → flat list of interactable/labelled elements.

    After collecting all candidates, we suppress *wrapping* clickable
    containers: if clickable A strictly contains clickable B, drop A.
    Reason: Android product cards are often a clickable RecyclerView item
    wrapping a clickable ADD button. Listing the outer card tempts the
    model to tap the card's center (which opens the product detail page)
    when it meant to tap ADD. Leaf-preference forces the inner target.
    """
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
    return _suppress_wrapping_clickables(elements)


def _suppress_wrapping_clickables(elements: list[UiElement]) -> list[UiElement]:
    """Drop clickable elements that strictly contain a smaller clickable.

    A non-clickable container (e.g. a TextView used as a row label) is
    kept regardless — it can't be tapped, so it's not competing for the
    same coords.
    """
    if len(elements) < 2:
        return elements
    clickables = [e for e in elements if e.clickable]
    if len(clickables) < 2:
        return elements
    # Sort largest-first so we test parents before children.
    by_area = sorted(clickables, key=lambda e: e.area, reverse=True)
    suppressed: set[int] = set()
    for i, outer in enumerate(by_area):
        if id(outer) in suppressed:
            continue
        ox1, oy1, ox2, oy2 = outer.bounds
        for inner in by_area[i + 1:]:
            if inner.area >= outer.area:
                continue
            ix1, iy1, ix2, iy2 = inner.bounds
            # Inner strictly inside outer (touching edges still counts as
            # "inside" — a button flush against the card edge is fine).
            if ox1 <= ix1 and oy1 <= iy1 and ox2 >= ix2 and oy2 >= iy2:
                suppressed.add(id(outer))
                break
    return [e for e in elements if id(e) not in suppressed]


def to_prompt_section(xml: str) -> str:
    """Render the parsed tree as a short, model-readable text block.

    Empty string when the tree is empty or unparseable — caller can just
    interpolate it into the prompt without conditionals.

    Action-target elements (ADD button, qty steppers, checkout) are
    marked with an [ACTION] prefix so the model picks the inner button
    instead of a surrounding card. Clickable elements that look like
    category tiles or autocomplete suggestions — judged by the absence of
    a nearby [ACTION] button AND/OR a category-text pattern — get a
    [CATEGORY?] prefix so the model steers around them when the task is
    to add a product to cart.
    """
    elements = parse(xml)
    if not elements:
        return ""
    action_rows = _action_row_centres(elements)
    # Sort reading-order so the listing matches what the model sees visually.
    # Within the same row, put [ACTION] elements first so they catch the
    # model's eye before the row's label/text element.
    elements.sort(key=lambda e: (e.cy, 0 if e.is_action else 1, e.cx))
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
        prefix = _prompt_prefix(e, action_rows)
        lines.append(
            f"- {prefix}{label} at ({e.cx}, {e.cy})"
            f"{'  ' + attr_str if attr_str else ''}"
        )
    suffix = ""
    if len(elements) > _MAX_ELEMENTS:
        suffix = f"\n  ... and {len(elements) - _MAX_ELEMENTS} more (truncated)"
    return (
        "UI elements (from accessibility tree, center coords are tap targets; "
        "[ACTION] = primary button — prefer over surrounding cards; "
        "[CATEGORY?] = likely category tile/suggestion — do NOT tap when "
        "the task is to add a specific product; [LOCATION] = delivery/"
        "address header — NOT the search bar, do NOT tap when looking for "
        "the search field):\n"
        + "\n".join(lines)
        + suffix
    )


def _action_row_centres(elements: list[UiElement]) -> list[int]:
    """Vertical centres of every [ACTION] element on screen.

    A clickable card is considered a real product (not a category tile) if
    its vertical centre is within `_SAME_ROW_TOLERANCE_PX` of one of these.
    """
    return sorted({e.cy for e in elements if e.is_action})


def _prompt_prefix(e: UiElement, action_rows: list[int]) -> str:
    """Choose `[ACTION] `, `[LOCATION] `, `[CATEGORY?] `, or empty prefix."""
    if e.is_action:
        return "[ACTION] "
    # The delivery-location header is a top-of-screen clickable that's
    # frequently mistaken for the search bar. Flag explicitly.
    if e.looks_like_location_header:
        return "[LOCATION] "
    # Only clickable elements get the category warning — non-clickable
    # labels are decoration, not tap targets, so the warning is moot.
    if not e.clickable:
        return ""
    if _has_action_in_row(e.cy, action_rows):
        return ""
    # No nearby ADD button. If the text/id also looks category-shaped,
    # surface the warning. We deliberately keep this conservative — a
    # clickable element with a plain title and no nearby ADD might just
    # be a list item on a settings screen, which isn't a category.
    if e.looks_like_category_text:
        return "[CATEGORY?] "
    return ""


def _has_action_in_row(cy: int, action_rows: list[int]) -> bool:
    """True if any [ACTION] element shares a horizontal band with `cy`."""
    for row_cy in action_rows:
        if abs(row_cy - cy) <= _SAME_ROW_TOLERANCE_PX:
            return True
    return False


def is_coord_in_elements(
    x: int,
    y: int,
    elements: list[UiElement],
    margin: int = 20,
) -> bool:
    """True if (x, y) falls within (or within `margin` px of) some element.

    Used to reject model-hallucinated taps — coords the model claims point at
    a UI control but actually land in dead space. `margin` covers small
    rounding errors and gives the model a forgiveness zone equivalent to
    a thumb's width.

    If `elements` is empty, returns True — we have no tree to validate
    against, so we can't reject. Better to let the action through than to
    block every tap on screens where the dump failed.
    """
    if not elements:
        return True
    for e in elements:
        x1, y1, x2, y2 = e.bounds
        if (x1 - margin) <= x <= (x2 + margin) and (y1 - margin) <= y <= (y2 + margin):
            return True
    return False


def find_smallest_element_at(
    x: int,
    y: int,
    elements: list[UiElement],
    margin: int = 20,
) -> UiElement | None:
    """Return the smallest element whose (margin-expanded) bounds contain (x, y).

    "Smallest" by area — when a coord falls inside multiple nested elements
    (e.g. a label inside a clickable row), the most-specific one wins. Used
    by the intent-vs-element check to ask "what element did this tap
    actually hit?" rather than just "did it hit anything?".
    """
    if not elements:
        return None
    best: UiElement | None = None
    for e in elements:
        x1, y1, x2, y2 = e.bounds
        if not ((x1 - margin) <= x <= (x2 + margin) and (y1 - margin) <= y <= (y2 + margin)):
            continue
        if best is None or e.area < best.area:
            best = e
    return best


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
