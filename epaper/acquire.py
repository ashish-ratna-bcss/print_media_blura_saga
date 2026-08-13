"""
Page image acquisition.

Whatever the platform, the segmentation stage needs one thing: the highest
resolution full-page image available. Readwhere serves pages as tile pyramids,
Summit serves whole JPGs. This module normalises both into a single full-page
file on disk.

Resolution matters more than anything else downstream -- body text in an Indian
broadsheet is ~7pt, so below roughly 2000px page width OCR quality falls off a
cliff regardless of which engine you use.
"""

from __future__ import annotations

import os
import urllib.request

from PIL import Image

from .probe import UA

Image.MAX_IMAGE_PIXELS = None


def download(url: str, dest: str, timeout: int = 60) -> bool:
    """Fetch a binary asset to dest. Returns success."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
        if len(data) < 512:          # error pages masquerading as images
            return False
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except Exception:
        return False


def stitch_tiles(tiles: list, width: int, height: int, dest: str,
                 tmp_dir: str = None) -> bool:
    """
    Assemble a tile pyramid level into one page image.

    tiles: [{x, y, w, h, base, text}] as returned by Readwhere.tiles().

    Each grid position may carry two layers: an opaque JPEG background and a
    transparent PNG holding the editorial text. Both are composited -- the
    text layer is where every word on the page lives.
    """
    if not tiles:
        return False
    tmp_dir = tmp_dir or (dest + ".tiles")
    os.makedirs(tmp_dir, exist_ok=True)
    canvas = Image.new("RGB", (width, height), "white")

    placed = 0
    for i, t in enumerate(tiles):
        got = False
        # Background first, then the text layer alpha-composited over it.
        for layer, ext in (("base", "jpg"), ("text", "png")):
            url = t.get(layer)
            if not url:
                continue
            p = os.path.join(tmp_dir, "t%02d_%s.%s" % (i, layer, ext))
            if not (os.path.exists(p) or download(url, p)):
                continue
            try:
                with Image.open(p) as im:
                    if layer == "text":
                        rgba = im.convert("RGBA")
                        canvas.paste(rgba, (t["x"], t["y"]), rgba)
                    else:
                        canvas.paste(im.convert("RGB"), (t["x"], t["y"]))
                got = True
            except Exception:
                continue
        placed += 1 if got else 0

    if placed == 0:
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    canvas.save(dest, "JPEG", quality=92)

    for f in os.listdir(tmp_dir):
        os.remove(os.path.join(tmp_dir, f))
    os.rmdir(tmp_dir)
    return True


def page_path(root: str, paper: str, edition: str, date: str, page_no) -> str:
    """Stable on-disk layout: out/pages/<paper>/<edition>/<date>/p<NN>.jpg"""
    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))
    return os.path.join(root, "pages", safe(paper), safe(edition), safe(date),
                        "p%s.jpg" % str(page_no).zfill(2))


def image_size(path: str):
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:
        return (0, 0)
