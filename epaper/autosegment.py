"""
Geometric article segmentation -- no API key, no model.

Recursive XY-cut. At each node both projection profiles are measured, the axis
holding the widest whitespace valley wins, the block is split there, and each
half recurses. Alternating the axis is what makes it work on a broadsheet: a
single pass of horizontal bands fails the moment one column runs past a
horizontal boundary, which on a front page is always. Recursion splits that
column into its own sub-bands instead.

Leaves come out at roughly paragraph-column granularity, so they are merged
back into stories using type size: a leaf whose top carries large glyphs starts
a new article, and following leaves without their own headline are continuation
columns of it.

This is a draft-quality detector and is labelled as such in its output
(`detector: "geometric"`). It reasons about ink and whitespace only -- it
cannot tell a sidebar from its parent story, because that needs reading. Its
job is to give every page usable clippings with no API key, and to give the VLM
pass in segment.py something to be measured against.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None

ANALYSIS_W = 1400
INK_THRESHOLD = 185
MAX_DEPTH = 9


def _ink_mask(path: str):
    with Image.open(path) as im:
        W0, H0 = im.size
        g = im.convert("L").resize(
            (ANALYSIS_W, max(1, int(H0 * ANALYSIS_W / float(W0)))), Image.BILINEAR)
    return (np.asarray(g, dtype=np.uint8) < INK_THRESHOLD), (W0, H0)


def _valleys(profile: np.ndarray, min_len: int, blank: float):
    """(start, end, length) for each blank run of at least min_len."""
    out, start = [], None
    for i, v in enumerate(profile <= blank):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_len:
                out.append((start, i, i - start))
            start = None
    if start is not None and len(profile) - start >= min_len:
        out.append((start, len(profile), len(profile) - start))
    return out


def _glyph_height(block: np.ndarray) -> int:
    """
    Tallest run of consecutive inked rows -- a proxy for type size.

    Body text at analysis scale runs 6-14 rows per line; headlines 18-60.
    """
    if block.size == 0:
        return 0
    rows = (block.sum(axis=1) / float(max(1, block.shape[1]))) > 0.06
    best = run = 0
    for v in rows:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


def _pictorial(block: np.ndarray) -> bool:
    """Photographs and display adverts lack the periodic row profile of text."""
    if block.size == 0:
        return False
    row = block.sum(axis=1) / float(max(1, block.shape[1]))
    crossings = int(np.sum(np.diff((row > 0.18).astype(np.int8)) != 0))
    return row.mean() > 0.28 and crossings / max(1.0, block.shape[0]) * 1000.0 < 13


def _cut(ink, y0, y1, x0, x1, min_h, min_v, depth, out):
    """Recursive XY-cut; appends leaf boxes to out."""
    h, w = y1 - y0, x1 - x0
    if depth >= MAX_DEPTH or h < min_h * 3 or w < min_v * 3:
        out.append((y0, y1, x0, x1))
        return

    blk = ink[y0:y1, x0:x1]
    if blk.mean() < 0.008:
        return

    row = blk.sum(axis=1) / float(w)
    col = blk.sum(axis=0) / float(h)

    # Printed rules separate as firmly as whitespace does.
    hv = _valleys(np.where(row > 0.55, 0.0, row), min_h, 0.005)
    vv = _valleys(col, min_v, 0.005)

    # Ignore valleys touching an edge -- those are margins, not separators.
    hv = [v for v in hv if v[0] > 0 and v[1] < h]
    vv = [v for v in vv if v[0] > 0 and v[1] < w]

    best_h = max(hv, key=lambda v: v[2]) if hv else None
    best_v = max(vv, key=lambda v: v[2]) if vv else None

    # Normalise so the two axes are comparable, and bias to vertical: column
    # gutters are the stronger structural signal on a newspaper page.
    sh = best_h[2] / float(h) if best_h else 0.0
    sv = (best_v[2] / float(w)) * 1.35 if best_v else 0.0

    if sv >= sh and best_v:
        m = x0 + (best_v[0] + best_v[1]) // 2
        _cut(ink, y0, y1, x0, m, min_h, min_v, depth + 1, out)
        _cut(ink, y0, y1, m, x1, min_h, min_v, depth + 1, out)
    elif best_h:
        m = y0 + (best_h[0] + best_h[1]) // 2
        _cut(ink, y0, m, x0, x1, min_h, min_v, depth + 1, out)
        _cut(ink, m, y1, x0, x1, min_h, min_v, depth + 1, out)
    else:
        out.append((y0, y1, x0, x1))


def _merge(ink, leaves, body_h):
    """
    Rebuild stories from column leaves.

    A leaf whose top carries large type opens a new article; leaves to its
    right at the same vertical position and without their own headline are read
    as continuation columns and absorbed.
    """
    scored = []
    for (y0, y1, x0, x1) in leaves:
        top = ink[y0:y0 + max(6, int((y1 - y0) * 0.34)), x0:x1]
        scored.append({"box": [y0, y1, x0, x1],
                       "head": _glyph_height(top) >= max(16, int(body_h * 1.7))})

    scored.sort(key=lambda s: (s["box"][0], s["box"][2]))
    groups = []
    for s in scored:
        y0, y1, x0, x1 = s["box"]
        placed = False
        if not s["head"] and groups:
            for g in reversed(groups):
                gy0, gy1, gx0, gx1 = g["box"]
                # Same horizontal band, and adjacent on the right.
                overlap = min(y1, gy1) - max(y0, gy0)
                if overlap > 0.55 * min(y1 - y0, gy1 - gy0) and abs(x0 - gx1) < 0.04 * ink.shape[1]:
                    g["box"] = [min(y0, gy0), max(y1, gy1), min(x0, gx0), max(x1, gx1)]
                    placed = True
                    break
        if not placed:
            groups.append({"box": [y0, y1, x0, x1], "head": s["head"]})
    return groups


def detect_regions(page_path: str, page_no: int = 1) -> list:
    """Same output shape as segment.detect_regions(): bbox fractions + bbox_px."""
    ink, (W0, H0) = _ink_mask(page_path)
    H, W = ink.shape
    min_h = max(5, int(H * 0.0045))
    min_v = max(5, int(W * 0.007))

    leaves = []
    _cut(ink, 0, H, 0, W, min_h, min_v, 0, leaves)
    leaves = [l for l in leaves
              if (l[1] - l[0]) > H * 0.012 and (l[3] - l[2]) > W * 0.03
              and ink[l[0]:l[1], l[2]:l[3]].mean() > 0.012]
    if not leaves:
        return []

    # Median glyph height over all leaves approximates this paper's body size.
    body_h = int(np.median([_glyph_height(ink[a:b, c:d]) for a, b, c, d in leaves]) or 10)

    regions = []
    for i, g in enumerate(_merge(ink, leaves, body_h), 1):
        y0, y1, x0, x1 = g["box"]
        if (x1 - x0) < W * 0.05 or (y1 - y0) < H * 0.018:
            continue
        blk = ink[y0:y1, x0:x1]
        kind = "ad" if _pictorial(blk) else "article"
        if page_no and y1 < H * 0.13 and kind == "article" and (x1 - x0) > W * 0.5:
            kind = "masthead"
        regions.append({
            "id": i, "type": kind,
            "bbox": [round(x0 / W, 4), round(y0 / H, 4),
                     round(x1 / W, 4), round(y1 / H, 4)],
            "bbox_px": [int(x0 / W * W0), int(y0 / H * H0),
                        int(x1 / W * W0), int(y1 / H * H0)],
            "columns": None, "has_photo": None, "continued_on_page": None,
            "headline_visible": "", "detector": "geometric",
        })
    return regions
