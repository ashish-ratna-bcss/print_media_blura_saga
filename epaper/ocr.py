"""
Headline OCR via IndicOCR only (BluraSaga OCR_SERVICE_URL).

YOLO has already isolated a headline strip; this module recognises that crop.
Body text is read later by Qwen2.5-VL — not here.

Environment:
    EPAPER_INDICOCR_URL    IndicOCR base (e.g. http://101.53.138.142:8000)
    EPAPER_OCR_URL         alias
    OCR_SERVICE_URL        BluraSaga alias
    EPAPER_HEADLINE        ocr (default) | vlm
                           vlm = skip IndicOCR and let local_backend use Qwen
                           for the headline crop (emergency only)
"""

from __future__ import annotations

import logging
import os
import re

from . import config

config.load_env()

log = logging.getLogger("epaper.ocr")

MODE = os.environ.get("EPAPER_HEADLINE", "ocr").lower()
OCR_URL = (
    os.environ.get("EPAPER_INDICOCR_URL")
    or os.environ.get("EPAPER_OCR_URL")
    or os.environ.get("OCR_SERVICE_URL")
    or ""
).rstrip("/")

_JUNK = re.compile(r"[|_~^`]+")
FUSED_LONG = 15
_FUSED_CASE = re.compile(r"[a-z][A-Z]")


def clean(text: str) -> str:
    """Tidy recognised headline text without inventing content."""
    t = _JUNK.sub(" ", text or "")
    t = " ".join(t.split())
    return t.strip(" -–—·.,")


def read_headline_detail(image, lang: str | None = None) -> dict:
    """
    Recognise one headline crop. Returns {text, backend, error, empty}.

    `lang` is accepted for API compatibility with callers; IndicOCR detects
    language server-side.
    """
    del lang  # IndicOCR handles language; keep signature stable for callers.
    result = {"text": "", "backend": "indicocr", "error": "", "empty": True}
    if not OCR_URL:
        result["error"] = "EPAPER_INDICOCR_URL is not set"
        return result
    try:
        from . import indic_ocr
        out = clean(indic_ocr.read_text(image))
        if out:
            result["text"] = out
            result["empty"] = False
            return result
        result["error"] = "IndicOCR returned empty text"
        return result
    except Exception as ex:
        result["error"] = str(ex)[:200]
        log.warning("IndicOCR headline failed: %s", result["error"])
        return result


def read_headline(image, psm: int = 7, lang: str | None = None) -> str:
    """Headline text only. Prefer read_headline_detail for diagnostics."""
    del psm
    return read_headline_detail(image, lang=lang).get("text") or ""


def looks_unreliable(text: str) -> bool:
    """Heuristic for collapsed spacing / garbage (used by optional VLM retry)."""
    t = (text or "").strip()
    if len(t) < 8:
        return True
    words = t.split()
    letters = sum(c.isalpha() for c in t)
    if letters < len(t) * 0.55:
        return True
    if any(len(w) > FUSED_LONG for w in words):
        return True
    if any(_FUSED_CASE.search(w) for w in words):
        return True
    for w in words:
        if re.search(r"[^\W\d_]\d|\d[^\W\d_]", w):
            return True
    return False


def langs_for(lang: str | None = None) -> str:
    """Compatibility stub — IndicOCR does not take Tesseract lang packs."""
    del lang
    return "indicocr"


def available() -> dict:
    """Whether IndicOCR is configured (live probe is health())."""
    return {"ok": bool(OCR_URL), "error": "" if OCR_URL else "EPAPER_INDICOCR_URL not set",
            "backend": "indicocr", "endpoint": OCR_URL or None}


def health() -> dict:
    base = {"mode": MODE, "provider": "indicocr", "endpoint": OCR_URL or None}
    try:
        from . import indic_ocr
        h = indic_ocr.health()
        h.update(base)
        return h
    except Exception as ex:
        return {**base, "backend": "indicocr", "available": False,
                "error": str(ex)[:200]}
