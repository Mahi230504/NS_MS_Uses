"""Unit tests for the phash wrapper."""
from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from agent.phash import compute


def _png(color: tuple[int, int, int], size: int = 32) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_pattern(seed: int) -> bytes:
    img = Image.new("RGB", (32, 32), color=(255, 255, 255))
    px = img.load()
    x = (seed * 7) % 32
    y = (seed * 13) % 32
    for dx in range(4):
        for dy in range(4):
            px[(x + dx) % 32, (y + dy) % 32] = (0, 0, 0)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestCompute:
    def test_identical_bytes_produce_identical_hash(self) -> None:
        data = _png((128, 64, 200))
        assert compute(data) == compute(data)

    def test_distinct_patterns_produce_distinct_hashes(self) -> None:
        hashes = {compute(_png_pattern(s)) for s in range(8)}
        assert len(hashes) >= 4  # phash distinguishes at least half the patterns

    def test_invalid_bytes_raise(self) -> None:
        with pytest.raises(ValueError):
            compute(b"not an image at all")
