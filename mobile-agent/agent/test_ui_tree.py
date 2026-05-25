"""Unit tests for the UI accessibility-tree parser."""
from __future__ import annotations

from agent.ui_tree import UiElement, parse, to_prompt_section


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
