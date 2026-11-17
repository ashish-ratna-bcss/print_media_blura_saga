"""Shapely-backed grouping geometry helpers."""

from epaper import geometry


def test_horizontal_overlap_half():
    assert abs(geometry.horizontal_overlap_ratio(
        [0, 0, 100, 50], [50, 10, 150, 60]) - 0.5) < 1e-6


def test_horizontal_no_overlap():
    assert geometry.horizontal_overlap_ratio([0, 0, 10, 10], [20, 0, 30, 10]) == 0.0


def test_union_and_gap():
    assert geometry.union_bbox([[0, 0, 10, 10], [5, 5, 20, 20]]) == [0.0, 0.0, 20.0, 20.0]
    assert geometry.vertical_gap([0, 0, 100, 40], [10, 50, 90, 80]) == 10.0


def test_figure_area_ratio():
    r = geometry.figure_area_ratio([[0, 0, 50, 50]], [0, 0, 100, 100])
    assert 0.24 < r < 0.26


def test_same_column_rejects_pagewide_banner():
    # Narrow title on the right must not claim a page-wide top banner.
    title = [1553, 150, 1730, 193]
    banner = [51, 157, 1837, 562]
    assert geometry.horizontal_overlap_ratio(title, banner) > 0.9
    assert geometry.same_column(title, banner) is False


def test_same_column_accepts_body_under_title():
    title = [400, 100, 1100, 180]
    body = [420, 200, 1080, 600]
    assert geometry.same_column(title, body) is True


def test_same_column_pad_capped_for_wide_title():
    # Wide headline must not treat the neighbouring column as its own.
    wide_title = [445, 3061, 1111, 3105]
    neighbor = [1222, 3107, 1487, 3166]
    assert geometry.same_column(wide_title, neighbor) is False


def test_clip_box_to_column():
    title = [500, 100, 700, 150]
    wide = [100, 160, 1200, 400]
    clipped = geometry.clip_box_to_column(title, wide)
    assert clipped is not None
    assert clipped[0] >= 100
    assert clipped[2] - clipped[0] < 500
