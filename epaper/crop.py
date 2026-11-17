"""
Article crop reconstruction with Pillow.

Cuts one article region out of a full-resolution page image.
"""

from __future__ import annotations

import os

from PIL import Image


def crop_region(page_path: str, bbox_px: list, dest: str, pad: int = 24) -> str:
    """Cut an article out of the full-resolution page JPEG."""
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with Image.open(page_path) as im:
        x0, y0, x1, y1 = bbox_px
        box = (max(0, int(x0) - pad), max(0, int(y0) - pad),
               min(im.width, int(x1) + pad), min(im.height, int(y1) + pad))
        im.crop(box).save(dest, "JPEG", quality=94)
    return dest
