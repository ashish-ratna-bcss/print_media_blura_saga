"""
Modular page extraction pipeline with timed stages and status tracking.

Wraps existing modules (pdf_render, preprocess, local_backend, segment, schemas)
without replacing them. Web jobs can keep using local_backend.detect_regions
directly; scripts and tests should prefer `process_page`.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

from PIL import Image

from . import local_backend, ocr, preprocess, schemas
from .crop import crop_region

# Terminal / stage names from the product brief.
STAGE_PDF_RECEIVED = "PDF_RECEIVED"
STAGE_PDF_RENDERED = "PDF_RENDERED"
STAGE_LAYOUT_DETECTED = "LAYOUT_DETECTED"
STAGE_ARTICLES_GROUPED = "ARTICLES_GROUPED"
STAGE_HEADLINES_OCR = "HEADLINES_OCR_PROCESSED"
STAGE_CROPS = "ARTICLE_CROPS_CREATED"
STAGE_VLM = "VLM_EXTRACTION_COMPLETED"
STAGE_VALIDATED = "VALIDATION_COMPLETED"
STAGE_STORED = "STORED"

STATUS_OK = "ok"
STATUS_FAILED = "FAILED"
STATUS_PARTIAL = "PARTIALLY_COMPLETED"
STATUS_RETRYING = "RETRYING"


def _stage(name: str, status: str, t0: float, error: str = "",
           **meta) -> schemas.StageResult:
    return schemas.StageResult(
        name=name, status=status,
        elapsed_s=round(time.monotonic() - t0, 3),
        error=(error or "")[:400],
        meta=meta)


def process_page(
    page_path: str,
    *,
    page_no: int = 1,
    crops_dir: str = "",
    paper: str = "",
    date: str = "",
    lang: str | None = None,
    with_vlm: bool = True,
    debug: bool = False,
    conf: float = 0.20,
    progress: Optional[Callable[[str], None]] = None,
) -> schemas.PagePipelineResult:
    """
    Run layout → group/OCR headlines → crop → optional VLM for one page image.

    `page_path` must already be a rendered page JPEG (use pdf_render for PDFs).
    """
    def note(stage: str):
        if progress:
            try:
                progress(stage)
            except Exception:
                pass

    result = schemas.PagePipelineResult(
        page_path=page_path, page_no=page_no, status=STATUS_FAILED)
    if not page_path or not os.path.isfile(page_path):
        result.errors.append("page image not found")
        result.stages.append(_stage(STAGE_PDF_RENDERED, "failed", time.monotonic(),
                                    "page image not found"))
        return result

    crops_dir = crops_dir or os.path.join(os.path.dirname(page_path) or ".", "crops")
    os.makedirs(crops_dir, exist_ok=True)

    # Quality (non-destructive)
    t0 = time.monotonic()
    note("quality check")
    q = preprocess.quality_report(page_path)
    result.stages.append(_stage("IMAGE_QUALITY", STATUS_OK if q.get("ok") else "failed",
                                t0, q.get("error") or "", **{
                                    k: q.get(k) for k in
                                    ("width", "height", "brightness", "blur_var",
                                     "warnings")}))
    if q.get("ok"):
        result.width = int(q.get("width") or 0)
        result.height = int(q.get("height") or 0)
    else:
        with Image.open(page_path) as im:
            result.width, result.height = im.size

    # Layout
    t0 = time.monotonic()
    note(STAGE_LAYOUT_DETECTED)
    try:
        elements = local_backend.detect_elements(page_path, conf=conf)
        result.elements = elements
        result.stages.append(_stage(STAGE_LAYOUT_DETECTED, STATUS_OK, t0,
                                    n_elements=len(elements)))
        result.timings["layout_s"] = round(time.monotonic() - t0, 3)
    except Exception as ex:
        result.errors.append("layout: %s" % ex)
        result.stages.append(_stage(STAGE_LAYOUT_DETECTED, "failed", t0, str(ex)))
        return result

    # Group + headline OCR (inside group_articles)
    t0 = time.monotonic()
    note(STAGE_ARTICLES_GROUPED)
    try:
        arts = local_backend.group_articles(page_path, elements, lang=lang)
        with Image.open(page_path) as im:
            W, H = im.size
        for i, a in enumerate(arts, 1):
            x0, y0, x1, y1 = a["bbox_px"]
            a["id"] = i
            a["bbox"] = [round(x0 / W, 4), round(y0 / H, 4),
                         round(x1 / W, 4), round(y1 / H, 4)]
        ocr_errs = sum(1 for a in arts if a.get("ocr_error"))
        result.stages.append(_stage(
            STAGE_ARTICLES_GROUPED, STATUS_OK, t0,
            n_articles=len(arts), ocr_errors=ocr_errs))
        result.stages.append(_stage(
            STAGE_HEADLINES_OCR, STATUS_OK if ocr_errs == 0 else "failed", t0,
            ocr_errors=ocr_errs,
            ocr_health=ocr.health()))
        result.timings["group_ocr_s"] = round(time.monotonic() - t0, 3)
    except Exception as ex:
        result.errors.append("grouping: %s" % ex)
        result.stages.append(_stage(STAGE_ARTICLES_GROUPED, "failed", t0, str(ex)))
        return result

    if debug:
        try:
            dd = local_backend.debug_dir_for(paper or "page", date or "", page_no)
            os.makedirs(dd, exist_ok=True)
            with open(os.path.join(dd, "elements.json"), "w", encoding="utf-8") as fh:
                json.dump({"page": page_path, "elements": elements}, fh,
                          ensure_ascii=False, indent=1)
            with open(os.path.join(dd, "grouping.json"), "w", encoding="utf-8") as fh:
                json.dump({"page": page_path, "articles": arts}, fh,
                          ensure_ascii=False, indent=1)
        except Exception:
            pass

    # Crops
    t0 = time.monotonic()
    note(STAGE_CROPS)
    crop_rows = []
    try:
        for i, a in enumerate(arts, 1):
            name = "p%s_a%02d.jpg" % (str(page_no).zfill(2), i)
            dest = os.path.join(crops_dir, name)
            crop_region(page_path, a["bbox_px"], dest)
            a["crop"] = name
            a["crop_path"] = dest
            a["page_no"] = page_no
            crop_rows.append(a)
        result.stages.append(_stage(STAGE_CROPS, STATUS_OK, t0, n_crops=len(crop_rows)))
        result.timings["crop_s"] = round(time.monotonic() - t0, 3)
    except Exception as ex:
        result.errors.append("crop: %s" % ex)
        result.stages.append(_stage(STAGE_CROPS, "failed", t0, str(ex)))
        return result

    # VLM
    validated: list[schemas.ExtractedArticle] = []
    vlm_fail = 0
    if with_vlm:
        t0 = time.monotonic()
        note(STAGE_VLM)
        for a in crop_rows:
            if a.get("type") != "article":
                try:
                    validated.append(schemas.article_from_region(
                        a, seq=a.get("id") or 0, crop=a.get("crop") or "",
                        page_no=page_no))
                except Exception as ex:
                    result.errors.append("validate-non-article: %s" % ex)
                continue
            try:
                fields = local_backend.transcribe(a["crop_path"])
                a.update({
                    "headline": fields.get("headline") or a.get("headline_visible") or "",
                    "subheadline": fields.get("subheadline"),
                    "body": fields.get("body") or "",
                    "summary": fields.get("summary"),
                    "byline": fields.get("byline"),
                    "dateline": fields.get("dateline"),
                    "location": fields.get("location"),
                    "keywords": fields.get("keywords") or [],
                    "people": fields.get("people") or [],
                    "organizations": fields.get("organizations") or [],
                    "events": fields.get("events") or [],
                    "places": fields.get("places") or [],
                    "text_source": "vlm",
                    "vlm_raw": fields.get("raw"),
                })
                if a.get("ocr_error") and not a.get("headline_visible"):
                    a["extraction_status"] = "partial"
            except Exception as ex:
                vlm_fail += 1
                a["body"] = ""
                a["vlm_error"] = str(ex)[:200]
                a["extraction_status"] = "failed"
                result.errors.append("vlm a%s: %s" % (a.get("id"), ex))
            try:
                art = schemas.article_from_region(
                    a, seq=a.get("id") or 0, crop=a.get("crop") or "",
                    page_no=page_no)
                # Merge extended VLM fields
                for k in ("summary", "subheadline", "keywords", "people",
                          "organizations", "events", "places", "body",
                          "byline", "dateline", "location"):
                    if a.get(k) is not None:
                        setattr(art, k, a.get(k) if k != "keywords" else (a.get(k) or []))
                if a.get("headline"):
                    art.headline = a["headline"]
                art.text_source = a.get("text_source") or "vlm"
                art.extraction_status = a.get("extraction_status") or "ok"
                art.crop_path = a.get("crop_path") or ""
                validated.append(art)
            except Exception as ex:
                result.errors.append("validate: %s" % ex)
        result.stages.append(_stage(
            STAGE_VLM, STATUS_OK if vlm_fail == 0 else "failed", t0,
            n_ok=len(validated) - vlm_fail, n_failed=vlm_fail))
        result.timings["vlm_s"] = round(time.monotonic() - t0, 3)
    else:
        for a in crop_rows:
            try:
                validated.append(schemas.article_from_region(
                    a, seq=a.get("id") or 0, crop=a.get("crop") or "",
                    page_no=page_no))
            except Exception as ex:
                result.errors.append("validate: %s" % ex)
        result.stages.append(_stage(STAGE_VLM, "skipped", time.monotonic()))

    t0 = time.monotonic()
    result.articles = validated
    result.stages.append(_stage(STAGE_VALIDATED, STATUS_OK, t0,
                                n_articles=len(validated)))

    if result.errors and validated:
        result.status = STATUS_PARTIAL
    elif result.errors and not validated:
        result.status = STATUS_FAILED
    else:
        result.status = "COMPLETED"
    return result


def process_page_to_dict(*args, **kwargs) -> dict:
    """JSON-serialisable wrapper for jobs/scripts."""
    r = process_page(*args, **kwargs)
    return json.loads(r.model_dump_json())


def dump_result(result: schemas.PagePipelineResult, path: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(result.model_dump_json(indent=2))
    return path
