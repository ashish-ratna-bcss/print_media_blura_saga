"""
Article-grouping geometry helpers backed by Shapely.

Keeps the same title-anchored rules as local_backend._assign_to_titles, but
uses Shapely polygons for overlap, distance and union boxes.
"""

from __future__ import annotations

from shapely.geometry import box as shapely_box
from shapely.ops import unary_union


def as_box(bbox_px) -> "shapely.geometry.Polygon":
    """[x1,y1,x2,y2] → Shapely box (empty if invalid)."""
    x1, y1, x2, y2 = (float(v) for v in bbox_px)
    if x2 <= x1 or y2 <= y1:
        return shapely_box(0, 0, 0, 0)
    return shapely_box(x1, y1, x2, y2)


def horizontal_overlap_ratio(a, b) -> float:
    """
    Horizontal overlap of two boxes as a fraction of the narrower width.
    Same semantics as the previous _xov() helper.
    """
    A, B = as_box(a), as_box(b)
    if A.is_empty or B.is_empty:
        return 0.0
    # Intersect vertical strips (full height of each) projected on X.
    ax1, _, ax2, _ = A.bounds
    bx1, _, bx2, _ = B.bounds
    lo, hi = max(ax1, bx1), min(ax2, bx2)
    if hi <= lo:
        return 0.0
    return (hi - lo) / float(max(1.0, min(ax2 - ax1, bx2 - bx1)))


def horizontal_overlap_px(a, b) -> float:
    """Raw horizontal overlap width in pixels (0 if none)."""
    ax1, ax2 = float(a[0]), float(a[2])
    bx1, bx2 = float(b[0]), float(b[2])
    return max(0.0, min(ax2, bx2) - max(ax1, bx1))


def same_column(title_bbox, block_bbox, pad_frac: float = 0.35,
                pad_min: float = 28.0, pad_max: float = 90.0) -> bool:
    """
    True when block sits in the title's column band.

    Narrow-width overlap alone is not enough: a 180px headline fully overlaps
    a page-wide banner, so overlap/narrow ≈ 1.0 and the story swallows the
    whole top of the page. Require the block's centre (or a large fraction of
    its width) to fall inside an expanded title band.

    Pad is capped: a 700px-wide headline with pad_frac=0.4 otherwise reaches
    ~280px into the next column and steals that column's body / next-title cap.
    """
    tx1, _, tx2, _ = (float(v) for v in title_bbox)
    bx1, _, bx2, _ = (float(v) for v in block_bbox)
    tw = max(1.0, tx2 - tx1)
    bw = max(1.0, bx2 - bx1)
    pad = min(pad_max, max(pad_min, pad_frac * tw))
    cx = 0.5 * (bx1 + bx2)
    if (tx1 - pad) <= cx <= (tx2 + pad):
        return True
    overlap = horizontal_overlap_px(title_bbox, block_bbox)
    # Wide multi-column headline covering most of a body/figure column.
    if overlap / bw >= 0.45:
        return True
    # Block covers most of a wide title (body under a banner headline).
    if overlap / tw >= 0.55 and overlap / bw >= 0.20:
        return True
    return False


def clip_box_to_column(title_bbox, block_bbox, pad_frac: float = 0.45,
                       pad_min: float = 36.0, pad_max: float = 110.0) -> list[float] | None:
    """
    Intersect block with an expanded title column. Returns None if empty.
    Keeps tall figures from inflating article width across the page.
    """
    tx1, ty1, tx2, ty2 = (float(v) for v in title_bbox)
    bx1, by1, bx2, by2 = (float(v) for v in block_bbox)
    tw = max(1.0, tx2 - tx1)
    pad = min(pad_max, max(pad_min, pad_frac * tw))
    x1 = max(bx1, tx1 - pad)
    x2 = min(bx2, tx2 + pad)
    if x2 - x1 < 8:
        return None
    return [x1, by1, x2, by2]


def vertical_gap(title_bbox, other_bbox) -> float:
    """
    Dy from title bottom to other top (negative if other overlaps upward).
    """
    T, O = as_box(title_bbox), as_box(other_bbox)
    if T.is_empty or O.is_empty:
        return 1e9
    return float(O.bounds[1] - T.bounds[3])


def center_distance2(a, b) -> float:
    """Squared Euclidean distance between box centers."""
    A, B = as_box(a), as_box(b)
    if A.is_empty or B.is_empty:
        return 1e18
    return float(A.centroid.distance(B.centroid) ** 2)


def union_bbox(boxes: list) -> list[float]:
    """Union of pixel boxes → [x1,y1,x2,y2]."""
    polys = [as_box(b) for b in boxes if b]
    polys = [p for p in polys if not p.is_empty]
    if not polys:
        return [0.0, 0.0, 0.0, 0.0]
    u = unary_union(polys)
    x1, y1, x2, y2 = u.bounds
    return [float(x1), float(y1), float(x2), float(y2)]


def iou(a, b) -> float:
    """Intersection-over-union of two [x1,y1,x2,y2] boxes."""
    A, B = as_box(a), as_box(b)
    if A.is_empty or B.is_empty:
        return 0.0
    inter = A.intersection(B).area
    if inter <= 0:
        return 0.0
    union = A.area + B.area - inter
    return float(inter / union) if union > 0 else 0.0


def figure_area_ratio(member_boxes: list, article_bbox) -> float:
    """Fraction of article area covered by figure boxes."""
    art = as_box(article_bbox)
    if art.is_empty or art.area <= 0:
        return 0.0
    figs = [as_box(b) for b in member_boxes]
    figs = [p for p in figs if not p.is_empty]
    if not figs:
        return 0.0
    # Union first so overlapping figures don't inflate the ratio into a false ad.
    u = unary_union(figs)
    return float(u.intersection(art).area) / float(art.area)
