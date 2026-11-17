#!/usr/bin/env python3
"""
Probe IndicOCR at EPAPER_INDICOCR_URL (BluraSaga OCR_SERVICE_URL: POST /extract).

Usage:
  python scripts/test_indic_ocr.py
  python scripts/test_indic_ocr.py web/data/jobs/<id>/p01.jpg
  python scripts/test_indic_ocr.py --crop web/data/jobs/<id>/p04_a01.jpg
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from epaper import config  # noqa: E402
config.load_env()

from epaper import indic_ocr, ocr  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*", help="page or crop image paths")
    ap.add_argument("--crop", action="append", default=[],
                    help="extra crop path(s)")
    ap.add_argument("--json", action="store_true", help="print full JSON")
    args = ap.parse_args()

    print("endpoint:", indic_ocr.OCR_URL or "(unset)")
    print("extract:", (indic_ocr.OCR_URL + "/extract") if indic_ocr.OCR_URL else "")
    h = indic_ocr.health()
    print("health:", json.dumps({k: h.get(k) for k in
                                 ("configured", "available", "error", "raw")},
                                ensure_ascii=False))
    print("ocr.health:", json.dumps(ocr.health(), ensure_ascii=False)[:400])

    paths = list(args.images) + list(args.crop)
    if not paths:
        # default: a few existing job crops if present
        jobs = os.path.join(ROOT, "web", "data", "jobs")
        if os.path.isdir(jobs):
            for jid in sorted(os.listdir(jobs)):
                for name in ("p04_a01.jpg", "p05_a01.jpg", "p01.jpg"):
                    p = os.path.join(jobs, jid, name)
                    if os.path.isfile(p):
                        paths.append(p)
                if paths:
                    break
        paths = paths[:3]

    results = []
    for p in paths:
        if not os.path.isfile(p):
            print("missing:", p)
            continue
        print("---", p)
        t0 = time.monotonic()
        try:
            out = indic_ocr.extract(p)
            dt = time.monotonic() - t0
            row = {
                "path": p,
                "ok": True,
                "elapsed_s": round(dt, 3),
                "text": (out.get("text") or "")[:200],
                "language": out.get("language"),
                "confidence": out.get("confidence"),
                "n_boxes": len(out.get("bounding_boxes") or []),
                "boxes_sample": (out.get("bounding_boxes") or [])[:3],
                "meta": out.get("meta"),
            }
            print("  ok %.2fs lang=%s conf=%s boxes=%d text=%r"
                  % (dt, row["language"], row["confidence"],
                     row["n_boxes"], row["text"][:80]))
        except Exception as ex:
            dt = time.monotonic() - t0
            row = {"path": p, "ok": False, "elapsed_s": round(dt, 3),
                   "error": str(ex)[:300]}
            print("  FAIL %.2fs %s" % (dt, row["error"]))
        results.append(row)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    ok = sum(1 for r in results if r.get("ok"))
    print("done: %d/%d ok" % (ok, len(results)))
    return 0 if (h.get("available") or ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
