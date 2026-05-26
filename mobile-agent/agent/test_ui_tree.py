"""Unit tests for the UI accessibility-tree parser."""
from __future__ import annotations

from agent.ui_tree import (
    UiElement,
    find_smallest_element_at,
    is_coord_in_elements,
    parse,
    to_prompt_section,
)


_SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]" clickable="false" />
  <node index="1" text="Search atta, butter…" resource-id="com.blinkit:id/search_box"
        class="android.widget.EditText" bounds="[20,160][1060,260]" clickable="true" />
  <node index="2" text="Add" resource-id="com.blinkit:id/add_btn"
        class="android.widget.Button" bounds="[800,700][960,820]" clickable="true" />
  <node index="3" text="" content-desc="Cart"
        class="android.widget.ImageView" bounds="[40,2300][140,2380]" clickable="true" />
  <node index="4" text="Decoration only" class="android.widget.TextView"
        bounds="[100,400][500,440]" clickable="false" />
  <node index="5" text="" class="android.widget.View" bounds="[0,0][0,0]" clickable="true" />
</hierarchy>
"""


class TestParse:
    def test_extracts_labelled_or_clickable(self) -> None:
        elements = parse(_SAMPLE_XML)
        labels = {e.text or e.desc or e.resource_id for e in elements}
        # Search input, Add button, Cart, decoration text — all kept.
        assert "Search atta, butter…" in labels
        assert "Add" in labels
        assert "Cart" in labels
        assert "Decoration only" in labels

    def test_skips_zero_area_nodes(self) -> None:
        elements = parse(_SAMPLE_XML)
        # The 0×0 clickable view at index 5 must not appear.
        assert all(e.bounds != (0, 0, 0, 0) for e in elements)

    def test_skips_root_unlabelled_layout(self) -> None:
        # The root FrameLayout has no text, no desc, no id, not clickable — drop it.
        elements = parse(_SAMPLE_XML)
        assert not any(e.class_name == "android.widget.FrameLayout" for e in elements)

    def test_centre_coords(self) -> None:
        elements = parse(_SAMPLE_XML)
        add_button = next(e for e in elements if e.text == "Add")
        assert add_button.cx == (800 + 960) // 2
        assert add_button.cy == (700 + 820) // 2

    def test_garbage_xml_returns_empty(self) -> None:
        assert parse("not xml") == []
        assert parse("") == []


class TestPromptSection:
    def test_renders_compact_listing(self) -> None:
        out = to_prompt_section(_SAMPLE_XML)
        # Should mention every kept element's label, center coord, key attrs.
        assert "Search atta" in out
        assert "(540, 210)" in out  # center of [20,160][1060,260]
        assert "search_box" in out  # short resource-id shown
        assert "EditText" in out
        # "Cart" only has content-desc → 'desc=' prefix expected.
        assert "desc=Cart" in out

    def test_empty_xml_yields_empty_string(self) -> None:
        assert to_prompt_section("") == ""
        assert to_prompt_section("garbage") == ""

    def test_caps_at_max_elements(self) -> None:
        # Build a huge tree and confirm we truncate gracefully.
        nodes = "".join(
            f'<node text="item{i}" class="android.widget.Button" '
            f'bounds="[10,{i*20}][100,{i*20+10}]" clickable="true" />'
            for i in range(120)
        )
        xml = f"<hierarchy rotation='0'>{nodes}</hierarchy>"
        out = to_prompt_section(xml)
        assert "truncated" in out


class TestIsCoordInElements:
    @staticmethod
    def _el(x1: int, y1: int, x2: int, y2: int) -> UiElement:
        return UiElement(
            text="x", desc="", resource_id="", class_name="",
            cx=(x1 + x2) // 2, cy=(y1 + y2) // 2,
            bounds=(x1, y1, x2, y2), clickable=True,
        )

    def test_inside_bounds(self) -> None:
        els = [self._el(100, 200, 300, 400)]
        assert is_coord_in_elements(200, 300, els)

    def test_outside_bounds_no_margin(self) -> None:
        els = [self._el(100, 200, 300, 400)]
        # Way outside — fails.
        assert not is_coord_in_elements(500, 600, els)

    def test_within_margin(self) -> None:
        # 10 px outside, margin 20 → passes.
        els = [self._el(100, 200, 300, 400)]
        assert is_coord_in_elements(310, 410, els)

    def test_just_outside_margin(self) -> None:
        els = [self._el(100, 200, 300, 400)]
        # 30 px outside, margin 20 → fails.
        assert not is_coord_in_elements(330, 200, els)

    def test_empty_elements_passes(self) -> None:
        # No tree → can't validate → don't reject.
        assert is_coord_in_elements(500, 500, [])

    def test_matches_any_element(self) -> None:
        els = [
            self._el(0, 0, 50, 50),
            self._el(500, 500, 600, 600),
        ]
        assert is_coord_in_elements(550, 550, els)


class TestFindSmallestElementAt:
    @staticmethod
    def _el(x1: int, y1: int, x2: int, y2: int, label: str = "x") -> UiElement:
        return UiElement(
            text=label, desc="", resource_id="", class_name="",
            cx=(x1 + x2) // 2, cy=(y1 + y2) // 2,
            bounds=(x1, y1, x2, y2), clickable=True,
        )

    def test_returns_none_on_empty(self) -> None:
        assert find_smallest_element_at(10, 10, []) is None

    def test_returns_none_when_no_hit(self) -> None:
        els = [self._el(0, 0, 50, 50, "a")]
        assert find_smallest_element_at(500, 500, els) is None

    def test_returns_smallest_when_nested(self) -> None:
        # Outer 0..200, inner 50..70. Coord (60, 60) falls in both — must
        # return the inner (smaller area) element.
        outer = self._el(0, 0, 200, 200, "outer")
        inner = self._el(50, 50, 70, 70, "inner")
        hit = find_smallest_element_at(60, 60, [outer, inner])
        assert hit is not None
        assert hit.text == "inner"

    def test_returns_single_match_when_no_nesting(self) -> None:
        els = [self._el(0, 0, 50, 50, "a"), self._el(100, 100, 200, 200, "b")]
        hit = find_smallest_element_at(150, 150, els)
        assert hit is not None
        assert hit.text == "b"

    def test_margin_extends_hit_region(self) -> None:
        els = [self._el(100, 100, 200, 200, "x")]
        # 10 px outside with default margin 20 → still a hit.
        assert find_smallest_element_at(210, 210, els) is not None


_CARD_WITH_ADD_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node class="androidx.recyclerview.widget.RecyclerView"
        resource-id="com.grofers.customerapp:id/product_card"
        bounds="[24,500][1056,900]" clickable="true" />
  <node text="Maggi 2-Minute Noodles Masala" class="android.widget.TextView"
        bounds="[60,540][800,580]" clickable="false" />
  <node text="ADD" resource-id="com.grofers.customerapp:id/add_to_cart"
        class="android.widget.Button" bounds="[860,720][960,780]" clickable="true" />
</hierarchy>
"""


class TestNestedClickableSuppression:
    def test_outer_card_dropped_when_inner_button_present(self) -> None:
        elements = parse(_CARD_WITH_ADD_XML)
        ids = {e.resource_id.split("/")[-1] for e in elements if e.resource_id}
        # Outer card is suppressed; ADD button kept.
        assert "product_card" not in ids
        assert "add_to_cart" in ids

    def test_non_clickable_label_preserved(self) -> None:
        elements = parse(_CARD_WITH_ADD_XML)
        # The product title TextView isn't clickable, so it isn't competing
        # with the ADD button — it must stay in the listing for the model
        # to identify what's in the card.
        labels = {e.text for e in elements}
        assert "Maggi 2-Minute Noodles Masala" in labels


_ACTION_BUTTONS_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="ADD" resource-id="com.x:id/add_to_cart" class="android.widget.Button"
        bounds="[860,720][960,780]" clickable="true" />
  <node text="+" resource-id="com.x:id/qty_plus" class="android.widget.ImageButton"
        bounds="[860,800][960,860]" clickable="true" />
  <node text="" content-desc="Add to cart" class="android.widget.ImageView"
        bounds="[200,800][260,860]" clickable="true" />
  <node text="Place Order" class="android.widget.Button"
        bounds="[100,2200][980,2280]" clickable="true" />
</hierarchy>
"""


class TestActionPrefix:
    def test_add_button_text_marked_as_action(self) -> None:
        out = to_prompt_section(_ACTION_BUTTONS_XML)
        # Each action button gets the [ACTION] prefix.
        assert "[ACTION] \"ADD\"" in out
        assert "[ACTION] \"+\"" in out
        assert "[ACTION] desc=Add to cart" in out
        assert "[ACTION] \"Place Order\"" in out

    def test_legend_in_header(self) -> None:
        out = to_prompt_section(_ACTION_BUTTONS_XML)
        # Header explains the [ACTION] marker so the model knows to prefer it.
        assert "[ACTION]" in out.splitlines()[0]


# A Blinkit search-results page where the first "Maggi" row is a category
# tile ("Maggi N Maggi House") sitting alone with no ADD button anywhere in
# its row band, and below it a real product card with an ADD button.
_BLINKIT_CATEGORY_VS_PRODUCT_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node text="Maggi N Maggi House"
        resource-id="com.grofers.customerapp:id/category_tile_title"
        class="android.widget.TextView"
        bounds="[40,1180][1040,1300]" clickable="true" />
  <node text="Maggi 2-Minute Masala Noodles 70g"
        class="android.widget.TextView"
        bounds="[40,1700][800,1760]" clickable="false" />
  <node text="ADD" resource-id="com.grofers.customerapp:id/add_to_cart"
        class="android.widget.Button"
        bounds="[860,1720][1020,1800]" clickable="true" />
</hierarchy>
"""


class TestCategoryPrefix:
    def test_category_tile_marked_when_no_nearby_action(self) -> None:
        # The "Maggi N Maggi House" row has no ADD button within ~250px
        # vertically — the only ADD on screen is at y≈1760, 500+ px below.
        # That plus the " N " text pattern → [CATEGORY?] marker.
        out = to_prompt_section(_BLINKIT_CATEGORY_VS_PRODUCT_XML)
        assert "[CATEGORY?]" in out
        # The category line must NOT also be tagged [ACTION].
        category_line = next(
            line for line in out.splitlines() if "Maggi N Maggi House" in line
        )
        assert "[CATEGORY?]" in category_line
        assert "[ACTION]" not in category_line

    def test_product_card_with_nearby_add_is_not_category(self) -> None:
        # The real product card text "Maggi 2-Minute Masala Noodles 70g" is
        # within ~250px of the ADD button → not category-flagged.
        out = to_prompt_section(_BLINKIT_CATEGORY_VS_PRODUCT_XML)
        product_line = next(
            line for line in out.splitlines() if "Masala Noodles" in line
        )
        assert "[CATEGORY?]" not in product_line

    def test_add_button_keeps_action_prefix(self) -> None:
        out = to_prompt_section(_BLINKIT_CATEGORY_VS_PRODUCT_XML)
        assert "[ACTION] \"ADD\"" in out

    def test_legend_mentions_category_marker(self) -> None:
        out = to_prompt_section(_BLINKIT_CATEGORY_VS_PRODUCT_XML)
        # Header explains the [CATEGORY?] marker so the model knows to avoid it.
        assert "[CATEGORY?]" in out.splitlines()[0]

    def test_location_header_marked(self) -> None:
        """A clickable element at the top of the screen with delivery/
        address text gets the [LOCATION] prefix so the model doesn't
        mistake it for the search bar."""
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="Delivering to Home · 8 mins"
        resource-id="com.grofers.customerapp:id/location_header"
        class="android.widget.TextView"
        bounds="[40,140][1040,260]" clickable="true" />
  <node text="Search for atta, butter…"
        resource-id="com.grofers.customerapp:id/search_box"
        class="android.widget.EditText"
        bounds="[40,290][1040,360]" clickable="true" />
</hierarchy>
"""
        out = to_prompt_section(xml)
        location_line = next(
            line for line in out.splitlines() if "Delivering to Home" in line
        )
        assert "[LOCATION]" in location_line
        # The real search bar must NOT be flagged as LOCATION.
        search_line = next(
            line for line in out.splitlines() if "Search for atta" in line
        )
        assert "[LOCATION]" not in search_line

    def test_location_marker_only_in_top_band(self) -> None:
        """A delivery-text element BELOW y≈300 isn't the home-screen
        location header — could be a list row on an address-picker
        screen. Don't mislabel it."""
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="Delivery address: Home"
        class="android.widget.TextView"
        bounds="[40,1500][1040,1580]" clickable="true" />
</hierarchy>
"""
        out = to_prompt_section(xml)
        data_lines = [line for line in out.splitlines() if line.startswith("- ")]
        assert all("[LOCATION]" not in line for line in data_lines)

    def test_non_clickable_label_never_category_tagged(self) -> None:
        # A non-clickable TextView with " N " in its name should NOT pick up
        # the [CATEGORY?] tag — non-clickable elements aren't tap targets so
        # the warning would be noise. (The header legend itself mentions
        # [CATEGORY?], so check only the data lines.)
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="Eggs N Bread" class="android.widget.TextView"
        bounds="[40,500][800,560]" clickable="false" />
</hierarchy>
"""
        out = to_prompt_section(xml)
        data_lines = [line for line in out.splitlines() if line.startswith("- ")]
        assert all("[CATEGORY?]" not in line for line in data_lines)
