"""Unit tests for the UI accessibility-tree parser."""
from __future__ import annotations

from pathlib import Path

from agent.ui_tree import (
    UiElement,
    find_action_at,
    find_smallest_element_at,
    has_focused_text_input,
    is_coord_in_elements,
    parse,
    to_prompt_section,
)


_FIXTURES = Path(__file__).parent / "fixtures"


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


class TestHasFocusedTextInput:
    @staticmethod
    def _el(class_name: str, focused: bool) -> UiElement:
        return UiElement(
            text="", desc="", resource_id="", class_name=class_name,
            cx=0, cy=0, bounds=(0, 0, 100, 100),
            clickable=True, focused=focused,
        )

    def test_empty_list_returns_true(self) -> None:
        # No tree available → conservatively allow typing.
        assert has_focused_text_input([])

    def test_no_focused_input_returns_false(self) -> None:
        els = [
            self._el("android.widget.EditText", focused=False),
            self._el("android.widget.Button", focused=True),  # focused but not input
        ]
        assert not has_focused_text_input(els)

    def test_focused_edittext_returns_true(self) -> None:
        els = [self._el("android.widget.EditText", focused=True)]
        assert has_focused_text_input(els)

    def test_focused_searchview_returns_true(self) -> None:
        els = [self._el("androidx.appcompat.widget.SearchView", focused=True)]
        assert has_focused_text_input(els)

    def test_focused_autocomplete_returns_true(self) -> None:
        els = [self._el("android.widget.AutoCompleteTextView", focused=True)]
        assert has_focused_text_input(els)

    def test_focused_attribute_parsed_from_xml(self) -> None:
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="" class="android.widget.EditText" '
            'bounds="[0,0][100,100]" clickable="true" focused="true" />'
            "</hierarchy>"
        )
        elements = parse(xml)
        assert any(e.focused for e in elements)
        assert has_focused_text_input(elements)


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

    def test_bare_add_in_content_desc_marked_action(self) -> None:
        """Regression: Blinkit's ADD button is text='', content-desc='ADD'
        on a custom android.view.View. Substring match against 'add to
        cart' missed it; the exact-desc set picks it up now."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="" content-desc="ADD" '
            'resource-id="com.grofers.customerapp:id/tv_title" '
            'class="android.view.View" '
            'bounds="[213,1527][348,1575]" clickable="true" />'
            "</hierarchy>"
        )
        out = to_prompt_section(xml)
        assert "[ACTION] desc=ADD" in out

    def test_action_elements_never_truncated(self) -> None:
        """Regression: Blinkit's search-results tree has ~270 elements, the
        ADD buttons live at y>1200, and the old fixed-cap truncation
        sorted by y dropped them entirely. The new policy reserves budget
        for every [ACTION] element so they always appear in the prompt."""
        # Build a tree with 80 decorative TextViews above (non-action) and
        # 3 ADD buttons at the bottom. Cap is 60.
        rows = []
        for i in range(80):
            rows.append(
                f'<node text="Decor {i}" '
                f'class="android.widget.TextView" '
                f'bounds="[0,{100 + i * 5}][100,{105 + i * 5}]" />'
            )
        for i, x in enumerate((200, 500, 800)):
            rows.append(
                f'<node text="" content-desc="ADD" '
                f'class="android.view.View" '
                f'bounds="[{x},1500][{x + 100},1550]" clickable="true" />'
            )
        xml = "<hierarchy rotation='0'>" + "".join(rows) + "</hierarchy>"
        out = to_prompt_section(xml)
        # All three ADDs present despite cap.
        assert out.count("[ACTION] desc=ADD") == 3
        # The "and N more (truncated)" footer shows non-action elements
        # were dropped, not the ADDs.
        assert "truncated" in out

    def test_add_to_wishlist_desc_NOT_marked_action(self) -> None:
        """Counter-test: 'Add to wishlist' must NOT be tagged [ACTION].
        Tapping wishlist is not what we want when 'add to cart' was the
        intent."""
        xml = (
            "<hierarchy rotation='0'>"
            '<node text="" content-desc="Add to wishlist" '
            'class="android.widget.ImageView" '
            'bounds="[612,1165][684,1237]" clickable="true" />'
            "</hierarchy>"
        )
        out = to_prompt_section(xml)
        # Wishlist must appear in the listing (it's clickable) but NOT
        # with the [ACTION] prefix.
        line = next(l for l in out.splitlines() if "Add to wishlist" in l)
        assert "[ACTION]" not in line


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


class TestContainerLabel:
    def test_action_inherits_ancestor_content_desc(self) -> None:
        # A nested ADD button inside a product card whose container has the
        # full title as content-desc should pick that up.
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.ViewGroup" bounds="[36,1153][348,2121]"
        content-desc="Hen Fruit -10 Max Protein Speciality Eggs is available for 130"
        clickable="false">
    <node class="android.view.ViewGroup" bounds="[213,1527][348,1575]"
          resource-id="com.x:id/stepper" clickable="false">
      <node class="android.view.View" content-desc="ADD"
            bounds="[213,1527][348,1575]" clickable="true" />
    </node>
  </node>
</hierarchy>
"""
        els = parse(xml)
        actions = [e for e in els if e.is_action]
        assert len(actions) == 1
        # "is available for ..." price suffix gets stripped — model only needs
        # the brand + SKU portion to match against the user's task.
        assert actions[0].container_label == \
            "Hen Fruit -10 Max Protein Speciality Eggs"

    def test_action_with_no_meaningful_ancestor_has_empty_label(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.Button" text="ADD" bounds="[100,200][300,260]"
        clickable="true" />
</hierarchy>
"""
        els = parse(xml)
        actions = [e for e in els if e.is_action]
        assert len(actions) == 1
        assert actions[0].container_label == ""

    def test_action_skips_screen_root_ancestor(self) -> None:
        # A root container spanning the whole screen with a global
        # content-desc should NOT be picked as the action's product label.
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]"
        content-desc="Some Activity Root Page" clickable="false">
    <node class="android.widget.Button" text="ADD" bounds="[100,200][300,260]"
          clickable="true" />
  </node>
</hierarchy>
"""
        els = parse(xml)
        actions = [e for e in els if e.is_action]
        assert actions[0].container_label == ""

    def test_action_prefers_closest_meaningful_ancestor(self) -> None:
        # When multiple ancestors carry content-desc, the innermost
        # meaningful one wins (the deepest product card, not the outer
        # carousel).
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" bounds="[0,500][1080,2000]"
        content-desc="Beer carousel" clickable="false">
    <node class="android.view.ViewGroup" bounds="[40,800][340,1800]"
          content-desc="Coolberg Cranberry Non-Alcoholic Beer" clickable="false">
      <node class="android.view.View" content-desc="ADD"
            bounds="[100,1500][300,1570]" clickable="true" />
    </node>
  </node>
</hierarchy>
"""
        els = parse(xml)
        actions = [e for e in els if e.is_action]
        assert actions[0].container_label == "Coolberg Cranberry Non-Alcoholic Beer"

    def test_action_skips_ancestor_whose_desc_is_just_add(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.ViewGroup" bounds="[40,500][340,800]"
        content-desc="Product A Title" clickable="false">
    <node class="android.view.ViewGroup" bounds="[100,600][300,700]"
          content-desc="ADD" clickable="false">
      <node class="android.view.View" content-desc="ADD"
            bounds="[100,600][300,700]" clickable="true" />
    </node>
  </node>
</hierarchy>
"""
        els = parse(xml)
        actions = [e for e in els if e.is_action]
        assert actions[0].container_label == "Product A Title"

    def test_prompt_section_renders_container_label_for_action(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.ViewGroup" bounds="[36,1153][348,2121]"
        content-desc="Hen Fruit -10 Max Protein Speciality Eggs"
        clickable="false">
    <node class="android.view.View" content-desc="ADD"
          bounds="[213,1527][348,1575]" clickable="true" />
  </node>
</hierarchy>
"""
        out = to_prompt_section(xml)
        assert 'for "Hen Fruit -10 Max Protein Speciality Eggs"' in out


class TestRidOnlyNoiseFilter:
    def test_rid_only_non_clickable_dropped(self) -> None:
        # A pure layout wrapper (FrameLayout with resource-id but no text,
        # no desc, no clickable, no focused) should be dropped — it has no
        # signal for the model.
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.FrameLayout" resource-id="com.x:id/frame_layout"
        bounds="[0,0][1080,400]" clickable="false" />
  <node text="Real label" class="android.widget.TextView"
        bounds="[100,500][500,560]" clickable="false" />
</hierarchy>
"""
        els = parse(xml)
        rids = [e.resource_id for e in els]
        assert "com.x:id/frame_layout" not in rids
        assert any(e.text == "Real label" for e in els)

    def test_focused_unlabelled_still_kept(self) -> None:
        # Rare but real: a focused EditText with no text yet, only a
        # resource-id, must still appear so has_focused_text_input sees it.
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.widget.EditText" resource-id="com.x:id/search_box"
        bounds="[100,200][900,260]" clickable="true" focused="true" />
</hierarchy>
"""
        els = parse(xml)
        assert len(els) == 1
        assert els[0].focused is True


class TestFindActionAt:
    def test_returns_smallest_action_under_coords(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.View" content-desc="ADD"
        bounds="[200,1260][330,1310]" clickable="true" />
  <node class="android.view.View" content-desc="ADD"
        bounds="[500,1260][630,1310]" clickable="true" />
</hierarchy>
"""
        els = parse(xml)
        hit = find_action_at(265, 1287, els)
        assert hit is not None
        assert hit.bounds == (200, 1260, 330, 1310)
        # A coord on the second ADD picks the second one.
        hit2 = find_action_at(565, 1287, els)
        assert hit2 is not None
        assert hit2.bounds == (500, 1260, 630, 1310)

    def test_returns_none_for_coords_off_any_action(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.View" content-desc="ADD"
        bounds="[200,1260][330,1310]" clickable="true" />
</hierarchy>
"""
        els = parse(xml)
        assert find_action_at(700, 700, els) is None


class TestRealFailingRunFixture:
    """Regression tests built from the actual failing run on the eggs
    search-results page (2026-05-26). The model tapped ADD at (265, 1287)
    five times claiming it was the Hen Fruit egg ADD — but that ADD is
    structurally for Coolberg Cranberry Non-Alcoholic Beer. The label
    enrichment must make this visible to the model.
    """

    def _xml(self) -> str:
        return (_FIXTURES / "blinkit_eggs_search_results.xml").read_text()

    def test_egg_add_label_is_correctly_attributed(self) -> None:
        els = parse(self._xml())
        hen_fruit_add = find_action_at(280, 1551, els)
        assert hen_fruit_add is not None
        assert hen_fruit_add.container_label == \
            "Hen Fruit -10 Max Protein Speciality Eggs"

    def test_beer_add_is_not_misattributed_as_egg(self) -> None:
        els = parse(self._xml())
        beer_add = find_action_at(265, 1287, els)
        assert beer_add is not None
        # The crucial assertion: this ADD is NOT Hen Fruit / Nutri Hatch /
        # Table White — it is a beer cross-sell card.
        assert "egg" not in beer_add.container_label.lower()
        assert "beer" in beer_add.container_label.lower()
        assert beer_add.container_label == \
            "Coolberg Cranberry Non-Alcoholic Beer"

    def test_all_visible_adds_have_labels(self) -> None:
        # Every [ACTION] ADD on the eggs search-results page should have a
        # product label attached — otherwise the model is grounded for some
        # but flying blind for others, which is the exact hallucination
        # surface we're closing.
        els = parse(self._xml())
        adds = [e for e in els if e.is_action]
        assert len(adds) == 6
        for a in adds:
            assert a.container_label, (
                f"ADD at ({a.cx},{a.cy}) has no container_label — model "
                f"would have no way to know which product this ADD buys"
            )

    def test_prompt_includes_all_product_labels(self) -> None:
        out = to_prompt_section(self._xml())
        # Each of the three visible egg products must surface in the
        # rendered prompt — otherwise the model can't pick the right ADD.
        for product in (
            "Hen Fruit",
            "Nutri Hatch",
            "Table White",
        ):
            assert product in out, f"missing {product!r} in prompt"

    def test_rid_only_layout_noise_filtered(self) -> None:
        # The pre-fix parse returned 272 elements (156 of which were rid-
        # only layout wrappers); the new filter should drop those, leaving
        # roughly half. Concrete count is brittle but a 200+ count would
        # mean the filter regressed.
        els = parse(self._xml())
        assert len(els) < 200, f"{len(els)} elements — filter regressed?"
        rid_only_noise = [
            e for e in els
            if not e.text and not e.desc and not e.clickable and not e.focused
        ]
        assert rid_only_noise == [], (
            f"rid-only noise leaked through: {rid_only_noise[:5]}"
        )


class TestCartBarDetection:
    def test_clickable_with_view_cart_text_tagged(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="View cart" resource-id="com.x:id/view_cart"
        class="android.widget.TextView" bounds="[465,1726][675,1785]"
        clickable="true" />
</hierarchy>
"""
        els = parse(xml)
        cart = [e for e in els if e.is_cart_bar]
        assert len(cart) == 1
        out = to_prompt_section(xml)
        assert "[CART]" in out

    def test_clickable_parent_with_view_cart_child_tagged(self) -> None:
        # The real Blinkit shape: clickable container with a non-clickable
        # TextView child carrying the "View cart" label. The container is
        # the tap target and must get the [CART] tag.
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.ViewGroup" resource-id="com.x:id/container"
        bounds="[231,1698][849,1860]" clickable="true">
    <node text="View cart" resource-id="com.x:id/view_cart"
          class="android.widget.TextView" bounds="[465,1726][675,1785]"
          clickable="false" />
  </node>
</hierarchy>
"""
        els = parse(xml)
        # Both nodes parsed (the container is clickable, the text has a label).
        container = next(e for e in els if e.bounds == (231, 1698, 849, 1860))
        assert container.is_cart_bar
        out = to_prompt_section(xml)
        # The container line — the actual tap target — must carry [CART].
        cart_lines = [
            line for line in out.splitlines()
            if "[CART]" in line and "at (540, 1779)" in line
        ]
        assert cart_lines, f"missing tagged container line. prompt:\n{out}"

    def test_non_clickable_view_cart_label_alone_not_tagged(self) -> None:
        # A bare text label without a clickable ancestor isn't a tap target;
        # we shouldn't tag it as a cart bar — there's nothing to tap.
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node text="View cart" class="android.widget.TextView"
        bounds="[100,100][300,140]" clickable="false" />
</hierarchy>
"""
        els = parse(xml)
        assert not any(e.is_cart_bar for e in els)

    def test_id_token_alone_tags_element(self) -> None:
        xml = """<?xml version='1.0' encoding='UTF-8'?>
<hierarchy rotation="0">
  <node class="android.view.ViewGroup" resource-id="com.x:id/mini_cart_widget"
        bounds="[0,2000][1080,2100]" clickable="true" />
</hierarchy>
"""
        els = parse(xml)
        assert any(e.is_cart_bar for e in els)

    def test_cart_bar_pinned_through_truncation(self) -> None:
        # If a cart bar exists but the screen has 200 other elements, the
        # cart bar must survive the 60-element truncation budget.
        nodes = "".join(
            f'<node text="row{i}" class="android.widget.TextView" '
            f'bounds="[10,{50+i*5}][100,{55+i*5}]" clickable="true" />'
            for i in range(200)
        )
        nodes += (
            '<node class="android.view.ViewGroup" '
            'resource-id="com.x:id/view_cart_widget" '
            'bounds="[0,2200][1080,2300]" clickable="true" />'
        )
        xml = f"<hierarchy rotation='0'>{nodes}</hierarchy>"
        out = to_prompt_section(xml)
        assert "[CART]" in out, "cart bar dropped through truncation"
