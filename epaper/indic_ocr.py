"""
IndicOCR client — same service BluraSaga calls via OCR_SERVICE_URL.

    Base:  EPAPER_INDICOCR_URL  (preferred)
           EPAPER_OCR_URL / OCR_SERVICE_URL  (aliases)
    Call:  POST {base}/extract
           Content-Type: application/json
           { "image_base64": "...", "tenant_key"?, "min_confidence"? }

    Health: GET {base}/health

Contract verified against BluraSaga docs/AI_SERVICES_GUIDE.md (same host :8000).
Boxes in the response are pixel [x1, y1, x2, y2] relative to the submitted image.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import time
import urllib.error
import urllib.request

from . import config

config.load_env()

log = logging.getLogger("epaper.indic_ocr")

# Prefer the explicit e-paper name; keep BluraSaga aliases so one .env works.
OCR_URL = (
    os.environ.get("EPAPER_INDICOCR_URL")
    or os.environ.get("EPAPER_OCR_URL")
    or os.environ.get("OCR_SERVICE_URL")
    or ""
).rstrip("/")

TENANT = (os.environ.get("EPAPER_OCR_TENANT") or "epaper").strip()
MIN_CONF = float(os.environ.get("EPAPER_OCR_MIN_CONF", "0.0"))
TIMEOUT = int(os.environ.get("EPAPER_OCR_TIMEOUT", "90"))
# Cap long edge before upload (newspaper pages ~2300×3900). Empty = no resize.
MAX_SIDE = int(os.environ.get("EPAPER_OCR_MAX_SIDE", "0") or 0)


class IndicOCRError(RuntimeError):
    pass


def configured() -> bool:
    return bool(OCR_URL)


def _encode(image, *, max_side: int = 0) -> tuple[str, tuple[int, int], float]:
    """
    PIL image or path → (base64 JPEG, (w, h) submitted, scale_to_original).

    scale_to_original maps submitted-image pixels back to the caller's image
    pixels (1.0 when no resize). Bounding boxes from the API are in submitted
    image coordinates and must be multiplied by this scale.
    """
    from PIL import Image

    owned = isinstance(image, str)
    im = Image.open(image) if owned else image
    try:
        rgb = im.convert("RGB")
        orig_w, orig_h = rgb.size
        scale = 1.0
        side = max_side or MAX_SIDE
        if side and max(orig_w, orig_h) > side:
            ratio = side / float(max(orig_w, orig_h))
            nw = max(1, int(round(orig_w * ratio)))
            nh = max(1, int(round(orig_h * ratio)))
            rgb = rgb.resize((nw, nh), Image.Resampling.LANCZOS)
            scale = orig_w / float(nw)
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode(), rgb.size, scale
    finally:
        if owned:
            im.close()


def _parse_box(raw) -> list[float] | None:
    """
    Normalize one box to [x1, y1, x2, y2] floats.

    Documented form: [x1, y1, x2, y2]. Also accept
    {x1,y1,x2,y2} / {x,y,w,h} / nested polygon [[x,y],...].
    """
    if raw is None:
        return None
    if isinstance(raw, dict):
        if all(k in raw for k in ("x1", "y1", "x2", "y2")):
            return [float(raw["x1"]), float(raw["y1"]),
                    float(raw["x2"]), float(raw["y2"])]
        if all(k in raw for k in ("x", "y", "w", "h")):
            x, y, w, h = (float(raw["x"]), float(raw["y"]),
                          float(raw["w"]), float(raw["h"]))
            return [x, y, x + w, y + h]
        return None
    if isinstance(raw, (list, tuple)) and len(raw) == 4 and all(
            isinstance(v, (int, float)) for v in raw):
        return [float(v) for v in raw]
    if isinstance(raw, (list, tuple)) and raw and isinstance(raw[0], (list, tuple)):
        xs = [float(p[0]) for p in raw if len(p) >= 2]
        ys = [float(p[1]) for p in raw if len(p) >= 2]
        if xs and ys:
            return [min(xs), min(ys), max(xs), max(ys)]
    return None


def normalize(data: dict, *, scale: float = 1.0,
              offset: tuple[float, float] = (0.0, 0.0)) -> dict:
    """
    Map IndicOCR `data` into the e-paper OCR shape.

    offset is added after scaling — use crop origin when the API ran on a
    crop and results must sit in full-page PyMuPDF / YOLO pixel space.
    """
    ox, oy = offset
    boxes_out = []
    for item in (data.get("bounding_boxes") or []):
        if not isinstance(item, dict):
            continue
        box = _parse_box(item.get("box") if "box" in item else item.get("bbox"))
        if not box:
            continue
        x1, y1, x2, y2 = [v * scale for v in box]
        conf = item.get("confidence")
        try:
            conf = float(conf) if conf is not None else None
        except (TypeError, ValueError):
            conf = None
        boxes_out.append({
            "bbox_px": [x1 + ox, y1 + oy, x2 + ox, y2 + oy],
            "text": (item.get("text") or "").strip(),
            "confidence": conf,
        })

    text = (data.get("full_text") or "").strip()
    if not text:
        vis = data.get("visible_text") or []
        if isinstance(vis, list):
            text = " ".join(str(t).strip() for t in vis if t).strip()
        elif isinstance(vis, str):
            text = vis.strip()

    conf = data.get("confidence")
    try:
        conf = float(conf) if conf is not None else None
    except (TypeError, ValueError):
        conf = None

    return {
        "text": text,
        "full_text": text,
        "bounding_boxes": boxes_out,
        "language": data.get("language"),
        "confidence": conf,
        "description": data.get("description"),
        "summary": data.get("summary"),
        "visible_text": data.get("visible_text") or [],
        "raw": data,
    }


def extract(image, *, min_confidence: float | None = None,
            tenant_key: str | None = None, timeout: int | None = None,
            offset: tuple[float, float] = (0.0, 0.0),
            max_side: int | None = None) -> dict:
    """
    Run IndicOCR on one image. Returns normalized e-paper OCR dict.

    Raises IndicOCRError on transport / service failure.
    """
    if not OCR_URL:
        raise IndicOCRError(
            "EPAPER_INDICOCR_URL (or EPAPER_OCR_URL / OCR_SERVICE_URL) is not set")

    b64, submitted_size, scale = _encode(
        image, max_side=MAX_SIDE if max_side is None else max_side)
    payload = {
        "image_base64": b64,
        "min_confidence": MIN_CONF if min_confidence is None else min_confidence,
    }
    key = tenant_key if tenant_key is not None else TENANT
    if key:
        payload["tenant_key"] = key

    url = OCR_URL + "/extract"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST")

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as ex:
        detail = ex.read()[:300].decode("utf-8", "ignore")
        # FortiGuard / HTML interstitials are not API errors worth retrying as JSON.
        if detail.lstrip().startswith("<"):
            raise IndicOCRError(
                "IndicOCR HTTP %s: blocked or non-API HTML from %s"
                % (ex.code, OCR_URL)) from ex
        raise IndicOCRError("IndicOCR HTTP %s: %s" % (ex.code, detail)) from ex
    except Exception as ex:
        raise IndicOCRError("IndicOCR unreachable at %s: %s"
                            % (OCR_URL, ex)) from ex

    elapsed = time.monotonic() - t0
    if raw.lstrip().startswith(b"<"):
        raise IndicOCRError(
            "IndicOCR returned HTML from %s (proxy/ fortiguard block?)" % OCR_URL)

    try:
        body = json.loads(raw.decode("utf-8", "ignore"))
    except json.JSONDecodeError as ex:
        raise IndicOCRError("IndicOCR invalid JSON: %s" % ex) from ex

    if not isinstance(body, dict) or not body.get("success"):
        err = body.get("error") if isinstance(body, dict) else None
        raise IndicOCRError(err or "IndicOCR success=false")

    data = body.get("data") or {}
    if not isinstance(data, dict):
        raise IndicOCRError("IndicOCR data is not an object")

    out = normalize(data, scale=scale, offset=offset)
    out["meta"] = {
        "endpoint": url,
        "elapsed_s": round(elapsed, 3),
        "submitted_size": list(submitted_size),
        "scale_to_original": scale,
        "offset": list(offset),
        "http_status": status,
        # never log tenant keys or base64
    }
    log.info("IndicOCR ok in %.2fs submitted=%sx%s boxes=%d text_len=%d",
             elapsed, submitted_size[0], submitted_size[1],
             len(out["bounding_boxes"]), len(out["text"]))
    return out


def read_text(image, **kwargs) -> str:
    """Best full_text / visible_text join for a crop."""
    return (extract(image, **kwargs).get("text") or "").strip()


def health() -> dict:
    st = {"backend": "indicocr", "endpoint": OCR_URL or None,
          "extract": (OCR_URL + "/extract") if OCR_URL else None,
          "configured": configured(), "available": False, "error": ""}
    if not OCR_URL:
        st["error"] = "EPAPER_INDICOCR_URL not set"
        return st
    try:
        req = urllib.request.Request(
            OCR_URL + "/health",
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "ignore")
        if raw.lstrip().startswith("<"):
            st["error"] = "OCR /health returned HTML (blocked or wrong host)"
            return st
        info = json.loads(raw)
        st["raw"] = {k: info.get(k) for k in
                     ("status", "model_loaded", "vlm_available", "vllm_model",
                      "ocr_engine", "active_engine") if k in info}
        st["available"] = bool(
            info.get("model_loaded") or info.get("vlm_available")
            or info.get("status") in ("ok", "healthy"))
        if not st["available"]:
            st["error"] = "service reported not ready"
    except Exception as ex:
        st["error"] = str(ex)[:200]
    return st
