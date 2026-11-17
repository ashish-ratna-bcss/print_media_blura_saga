"""Unit tests for pipeline schemas, OCR normalize, preprocess (no live GPU)."""

from __future__ import annotations

import os

import pytest

from epaper import indic_ocr, schemas
from epaper.preprocess import quality_report


def test_bbox_from_list_rejects_inverted():
    with pytest.raises(ValueError):
        schemas.BBoxPx.from_list([10, 10, 5, 20])


def test_validate_vlm_dict_coerces_lists():
    v = schemas.validate_vlm_dict({
        "headline": "Hello",
        "body": "Body text here",
        "keywords": "single",
        "people": None,
        "organizations": ["Org"],
    })
    assert v.headline == "Hello"
    assert v.keywords == ["single"]
    assert v.people == []
    assert v.organizations == ["Org"]
    assert v.raw is not None


def test_article_from_region_partial_ocr():
    art = schemas.article_from_region({
        "type": "article",
        "headline_visible": "",
        "bbox_px": [1, 2, 30, 40],
        "ocr_error": "timeout",
        "extraction_status": "partial",
        "elements": [1, 2],
    }, seq=3, crop="a03.jpg", page_no=1)
    assert art.seq == 3
    assert art.ocr_error == "timeout"
    assert art.extraction_status == "partial"
    assert art.n_elements == 2


def test_indic_normalize_scales_and_offsets():
    data = {
        "full_text": "HEAD",
        "confidence": 0.9,
        "language": "en",
        "bounding_boxes": [
            {"box": [10, 20, 30, 40], "text": "HEAD", "confidence": 0.95},
        ],
    }
    out = indic_ocr.normalize(data, scale=2.0, offset=(100, 200))
    assert out["text"] == "HEAD"
    assert out["bounding_boxes"][0]["bbox_px"] == [120.0, 240.0, 160.0, 280.0]
    assert out["bounding_boxes"][0]["confidence"] == 0.95


def test_indic_parse_xywh_dict():
    box = indic_ocr._parse_box({"x": 1, "y": 2, "w": 10, "h": 5})
    assert box == [1.0, 2.0, 11.0, 7.0]


def test_quality_report_missing():
    r = quality_report("/no/such/file.jpg")
    assert r["ok"] is False


def test_quality_report_sample_page():
    root = os.path.join(os.path.dirname(__file__), "..", "web", "data", "jobs")
    sample = None
    if os.path.isdir(root):
        for jid in os.listdir(root):
            p = os.path.join(root, jid, "p01.jpg")
            if os.path.isfile(p):
                sample = p
                break
    if not sample:
        pytest.skip("no job page images")
    r = quality_report(sample)
    assert r["ok"] is True
    assert r["width"] > 100
    assert r["height"] > 100
