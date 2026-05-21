"""Perceptual hash helper for screenshot dedup + outcome verification.

Wraps imagehash so the rest of the codebase doesn't import PIL directly. A
phash returned here is a stable string; equal strings mean "visually the same
screen" within the model's tolerance (~rendered animations, antialiasing).
"""
from __future__ import annotations

import io

import imagehash
from PIL import Image


_HASH_SIZE = 8  # 64-bit hash; cheap to compare and stable across minor jitter.


def compute(image_bytes: bytes) -> str:
    """Return a hex string phash for the given PNG/JPEG bytes.

    Raises ValueError if the bytes don't decode as an image.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            return str(imagehash.phash(img, hash_size=_HASH_SIZE))
    except Exception as e:
        raise ValueError(f"could not compute phash: {e}") from e
