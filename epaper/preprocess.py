"""
Optional page-image quality checks and light preprocessing.

The original page JPEG is never overwritten. Callers that want an enhanced
copy for YOLO/OCR pass a separate destination path.
"""

from __future__ import annotations

import os
from typing import Optional


def quality_report(path: str) -> dict:
    """
    Non-destructive checks: size, brightness, blur proxy, format.
    Does not modify the file.
    """
    from PIL import Image, ImageStat

    out = {"path": path, "ok": False, "error": "", "width": 0, "height": 0,
           "mode": "", "brightness": None, "blur_var": None, "warnings": []}
    if not path or not os.path.isfile(path):
        out["error"] = "image not found"
        return out
    try:
        with Image.open(path) as im:
            out["width"], out["height"] = im.size
            out["mode"] = im.mode
            rgb = im.convert("RGB")
            stat = ImageStat.Stat(rgb)
            out["brightness"] = round(sum(stat.mean) / 3.0, 1)
        try:
            import cv2
            bgr = cv2.imread(path)
            if bgr is not None:
                gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
                out["blur_var"] = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except Exception:
            out["blur_var"] = None

        if min(out["width"], out["height"]) < 400:
            out["warnings"].append("very_small_page")
        if out["brightness"] is not None and out["brightness"] < 40:
            out["warnings"].append("very_dark")
        if out["brightness"] is not None and out["brightness"] > 240:
            out["warnings"].append("very_bright")
        if out["blur_var"] is not None and out["blur_var"] < 20:
            out["warnings"].append("possibly_blurry")
        out["ok"] = True
    except Exception as ex:
        out["error"] = str(ex)[:200]
    return out


def enhance_copy(src: str, dest: str, *, contrast: float = 1.05,
                 sharpen: bool = False) -> dict:
    """
    Write a mildly enhanced copy for detection. Original untouched.

    Default contrast is slight; sharpening off by default so fine Devanagari /
    Telugu strokes are not damaged.
    """
    from PIL import Image, ImageEnhance, ImageFilter

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with Image.open(src) as im:
        rgb = im.convert("RGB")
        if abs(contrast - 1.0) > 1e-3:
            rgb = ImageEnhance.Contrast(rgb).enhance(contrast)
        if sharpen:
            rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1, percent=80, threshold=3))
        rgb.save(dest, "JPEG", quality=92)
    return {"src": src, "dest": dest, "contrast": contrast, "sharpen": sharpen}


def maybe_preprocess(page_path: str, out_dir: Optional[str] = None,
                     *, enable: bool | None = None) -> dict:
    """
    Run quality report; optionally write enhanced sibling when
    EPAPER_PREPROCESS=1 (or enable=True).
    """
    flag = enable
    if flag is None:
        flag = os.environ.get("EPAPER_PREPROCESS", "").strip() in ("1", "true", "yes")
    report = quality_report(page_path)
    report["enhanced_path"] = None
    if flag and report.get("ok"):
        base = out_dir or os.path.dirname(page_path) or "."
        name = os.path.splitext(os.path.basename(page_path))[0] + "_prep.jpg"
        dest = os.path.join(base, name)
        enhance_copy(page_path, dest)
        report["enhanced_path"] = dest
    return report
