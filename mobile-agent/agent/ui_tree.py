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
# Exact (case-insensitive) matches for content-desc. Real-world example:
# Blinkit's ADD button is a custom android.view.View with text="" and
# content-desc="ADD" — substring match against "add to cart" misses it.
# An exact-match set lets us tag bare "ADD" / "+" / "−" without also
# tagging "Add to wishlist" (which is exactly the kind of false positive
# we want to avoid).
_ACTION_DESC_EXACT = frozenset({"add", "+", "−", "-", "buy", "buy now",
                                "place order", "pay", "pay now", "checkout",
                                "proceed", "increase", "decrease"})

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

# View-cart / mini-cart bar tokens. Tagged with `[CART]` in the prompt so
# the model knows which element navigates to the cart screen after adding
# items. On Blinkit, post-ADD the floating bar sometimes isn't surfaced
# in the search-results a11y tree, but on home it IS labelled
# (id=view_cart, text="View cart") — surfacing it removes a hallucination
# surface where the model invents cart contents instead of navigating.
_CART_BAR_TEXT_TOKENS = (
    "view cart",
    "go to cart",
    "open cart",
    "view your cart",
)
_CART_BAR_DESC_TOKENS = _CART_BAR_TEXT_TOKENS + (
    "cart with",     # "cart with 1 item"
    "items in cart",
    "item in cart",
)
_CART_BAR_ID_TOKENS = (
    "view_cart",
    "mini_cart",
    "cart_bottom",
    "cart_widget",
    "floating_cart",
    "cart_bar",
    "go_to_cart",
)


def _is_action_like(text: str, desc: str, resource_id: str) -> bool:
    """True when text/desc/id together identify a primary action target.

    Pulled out as a free function so `parse()` can decide whether to look up
    a container label for the node BEFORE constructing the (frozen)
    UiElement. The instance property `UiElement.is_action` delegates to
    this same logic.
    """
    t = text.strip().lower()
    if t in _ACTION_TEXT_TOKENS:
        return True
    rid = resource_id.lower()
    if any(tok in rid for tok in _ACTION_ID_TOKENS):
        return True
    d = desc.strip().lower()
    # Exact-match first so bare "ADD" / "+" gets picked up without
    # matching the substring "add" inside "Add to wishlist".
    if d in _ACTION_DESC_EXACT:
        return True
    if any(tok in d for tok in _ACTION_DESC_TOKENS):
        return True
    return False


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
    focused: bool = False  # uiautomator's focused=true|false attribute
    # For [ACTION] elements: the closest XML ancestor's content-desc/text
    # that names the row this action belongs to (e.g. a product card's
    # full title for an ADD button). Empty when the element isn't an
    # action or no meaningful ancestor exists. Critical for grounding the
    # model — without it, six identical "ADD" buttons on a search results
    # screen tell the model nothing about which product is which.
    container_label: str = ""
    # True for clickable elements that ARE or CONTAIN a "View cart" / mini-
    # cart bar in their XML subtree. Computed during parsing (needs the
    # parent map / descendant walk). The label often lives on a non-
    # clickable child TextView, while the actual tap target is a clickable
    # parent ViewGroup with no useful label of its own — so checking only
    # the element's own attrs misses the real tap target.
    is_cart_bar: bool = False

    @property
    def has_label(self) -> bool:
        return bool(self.text or self.desc or self.resource_id)

    @property
    def is_action(self) -> bool:
        return _is_action_like(self.text, self.desc, self.resource_id)

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

    # Parent map: lets us walk up from an [ACTION] node to its enclosing
    # product card (whose content-desc is the only place the product title
    # lives in Blinkit's tree).
    parent_map = {child: parent for parent in root.iter() for child in parent}

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
        focused = attrs.get("focused", "false") == "true"
        text = (attrs.get("text") or "").strip()
        desc = (attrs.get("content-desc") or "").strip()
        rid = (attrs.get("resource-id") or "").strip()
        cls = (attrs.get("class") or "").strip()
        # Keep clickable, labelled, or focused elements. rid-only non-
        # clickable layout wrappers (FrameLayout, image_container,
        # background_overlay) carry no signal to the model — they're not
        # tappable and have no human-readable name — and they eat the
        # 60-element prompt budget. Focused-but-unlabelled inputs (rare)
        # still pass so the type-focus structural check sees them.
        if not (clickable or text or desc or focused):
            continue
        container_label = ""
        if _is_action_like(text, desc, rid):
            container_label = _find_container_label(node, parent_map)
        # Cart-bar detection: only clickable elements can be tap targets,
        # and a node qualifies if its own attrs OR any descendant's attrs
        # match the cart patterns. The label often lives on a non-
        # clickable TextView child while the actual tap target is a
        # generic clickable ViewGroup wrapping it.
        is_cart_bar = clickable and (
            _is_cart_node(text, desc, rid) or _has_cart_descendant(node)
        )
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
                focused=focused,
                container_label=container_label,
                is_cart_bar=is_cart_bar,
            )
        )
    return _suppress_wrapping_clickables(elements)


def _is_cart_node(text: str, desc: str, resource_id: str) -> bool:
    """True if this node's own attrs match a cart-bar token."""
    rid = resource_id.lower()
    if any(tok in rid for tok in _CART_BAR_ID_TOKENS):
        return True
    t = text.lower()
    if any(tok in t for tok in _CART_BAR_TEXT_TOKENS):
        return True
    d = desc.lower()
    if any(tok in d for tok in _CART_BAR_DESC_TOKENS):
        return True
    return False


def _has_cart_descendant(node) -> bool:
    """True if any descendant `<node>` in the XML matches cart-bar tokens."""
    for n in node.iter("node"):
        if n is node:
            continue
        attrs = n.attrib
        if _is_cart_node(
            (attrs.get("text") or "").strip(),
            (attrs.get("content-desc") or "").strip(),
            (attrs.get("resource-id") or "").strip(),
        ):
            return True
    return False


# Strip the trailing "is available for ₹130" suffix that Blinkit appends to
# product-card content-descs. Keeps brand+SKU but drops the redundant
# price line that the model can already see elsewhere.
_AVAILABLE_FOR_RE = re.compile(r"\s+is\s+available\s+for\s+₹?[\d,.]+.*$", re.IGNORECASE)
_MIN_CONTAINER_LABEL_LEN = 5
# Reject ancestors that span most of the screen — those are the activity
# root / outer RecyclerView, never a product card.
_DEFAULT_SCREEN_W = 1080
_DEFAULT_SCREEN_H = 2400
_MAX_CONTAINER_W = int(0.85 * _DEFAULT_SCREEN_W)
_MAX_CONTAINER_H = int(0.65 * _DEFAULT_SCREEN_H)


def _find_container_label(action_node, parent_map: dict) -> str:
    """Closest meaningful ancestor content-desc/text for an action node.

    Walks up the XML parent chain until it finds an ancestor whose
    content-desc (preferred) or text is non-empty, isn't itself just an
    action token ("ADD" / "+"), is at least `_MIN_CONTAINER_LABEL_LEN`
    chars, and lives in a container smaller than the screen root.

    Returns an empty string when no such ancestor exists (rendered
    without a "for …" annotation).
    """
    cur = parent_map.get(action_node)
    while cur is not None:
        attrs = cur.attrib
        for candidate in ((attrs.get("content-desc") or "").strip(),
                          (attrs.get("text") or "").strip()):
            if not candidate:
                continue
            if candidate.lower() in _ACTION_DESC_EXACT:
                continue
            if len(candidate) < _MIN_CONTAINER_LABEL_LEN:
                continue
            b = _parse_bounds(attrs.get("bounds", ""))
            if b is None:
                continue
            x1, y1, x2, y2 = b
            if (x2 - x1) > _MAX_CONTAINER_W and (y2 - y1) > _MAX_CONTAINER_H:
                continue
            return _AVAILABLE_FOR_RE.sub("", candidate).strip()
        cur = parent_map.get(cur)
    return ""


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
    # Truncation policy: always include EVERY [ACTION] element AND every
    # [CART] element. Real-world case: Blinkit's search-results page has
    # ~270 elements but the ADD buttons live at the bottom (y>1200). A
    # plain y-sorted truncation discards them, leaving the model unable
    # to find any ADD to tap. The cart-bar shares the same fate after
    # ADD (post-ADD it appears near the bottom). Action + cart elements
    # get a budget reservation; the remainder fills with the rest in
    # y-order. Total cap unchanged.
    pinned = [e for e in elements if e.is_action or e.is_cart_bar]
    others = [e for e in elements if not (e.is_action or e.is_cart_bar)]
    remaining_budget = max(0, _MAX_ELEMENTS - len(pinned))
    selected = set(map(id, pinned + others[:remaining_budget]))
    visible_elements = [e for e in elements if id(e) in selected]
    truncated_count = len(elements) - len(visible_elements)
    lines: list[str] = []
    for e in visible_elements:
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
        # For [ACTION] elements, append the product/row label pulled from
        # the XML ancestor chain. Without this, six identical "ADD" lines
        # tell the model nothing about which product each one buys —
        # exactly the hallucination loop we hit on Blinkit's eggs page.
        suffix_label = (
            f'  for "{_truncate(e.container_label, n=80)}"'
            if e.container_label else ""
        )
        lines.append(
            f"- {prefix}{label} at ({e.cx}, {e.cy})"
            f"{suffix_label}"
            f"{'  ' + attr_str if attr_str else ''}"
        )
    suffix = ""
    if truncated_count > 0:
        suffix = f"\n  ... and {truncated_count} more (truncated)"
    return (
        "UI elements (from accessibility tree, center coords are tap targets; "
        "[ACTION] = primary button — prefer over surrounding cards; "
        "[CART] = View Cart / mini-cart bar — tap this to navigate to the "
        "cart screen after a successful ADD; "
        "[CATEGORY?] = likely category tile/suggestion — do NOT tap when "
        "the task is to add a specific product; [LOCATION] = delivery/"
        "address header — NOT the search bar, do NOT tap when looking for "
        "the search field; for [ACTION] lines, the `for \"...\"` annotation "
        "names the product/row that ADD button buys — match it against the "
        "user's requested item before tapping):\n"
        + "\n".join(lines)
        + suffix
    )


def find_action_at(
    x: int,
    y: int,
    elements: list[UiElement],
    tolerance: int = 30,
) -> UiElement | None:
    """Return the [ACTION] element whose bounds (+tolerance) contain (x, y).

    The orchestrator's product-vs-coords check uses this to retrieve the
    container label for a tap claiming to be on a specific ADD button.
    Tolerance covers small model coord drift around the button edges.
    """
    if not elements:
        return None
    best: UiElement | None = None
    for e in elements:
        if not e.is_action:
            continue
        x1, y1, x2, y2 = e.bounds
        if not ((x1 - tolerance) <= x <= (x2 + tolerance) and
                (y1 - tolerance) <= y <= (y2 + tolerance)):
            continue
        if best is None or e.area < best.area:
            best = e
    return best


def _action_row_centres(elements: list[UiElement]) -> list[int]:
    """Vertical centres of every [ACTION] element on screen.

    A clickable card is considered a real product (not a category tile) if
    its vertical centre is within `_SAME_ROW_TOLERANCE_PX` of one of these.
    """
    return sorted({e.cy for e in elements if e.is_action})


def _prompt_prefix(e: UiElement, action_rows: list[int]) -> str:
    """Choose `[ACTION] `, `[CART] `, `[LOCATION] `, `[CATEGORY?] `, or empty."""
    if e.is_action:
        return "[ACTION] "
    # Mini-cart / View Cart bar: the model's go-to navigation target after
    # a successful ADD. Tag explicitly so it doesn't get lost among other
    # clickables and so the model can't hallucinate "the cart" from
    # unrelated [ACTION] product lines.
    if e.is_cart_bar:
        return "[CART] "
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


# Substrings (case-insensitive) in `class_name` that mark an element as a
# text-input. Used by `has_focused_text_input` to decide whether a `type`
# action will actually go into something.
_TEXT_INPUT_CLASS_TOKENS = ("edittext", "searchview", "autocompletetextview")


def has_focused_text_input(elements: list[UiElement]) -> bool:
    """True if any element has focused=true AND looks like a text input.

    Used by the orchestrator to reject `type` actions when the model
    hasn't actually focused an input — the typed characters would go
    nowhere (ADB `input text` requires a focused EditText to land).

    Conservatively also returns True when `elements` is empty: without a
    UI tree to inspect we can't prove the absence of a focused input,
    and it's better to let the type through than to falsely block it.
    """
    if not elements:
        return True
    for e in elements:
        if not e.focused:
            continue
        cls = e.class_name.lower()
        if any(tok in cls for tok in _TEXT_INPUT_CLASS_TOKENS):
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
