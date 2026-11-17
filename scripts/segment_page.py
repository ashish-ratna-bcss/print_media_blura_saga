#!/usr/bin/env python3
"""
Run the current AI pipeline on one page image.

    python scripts/segment_page.py path/to/page.jpg
    python scripts/segment_page.py path/to/page.jpg --vlm
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from epaper import config, pipeline  # noqa: E402

config.load_env()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("page", help="rendered page JPEG/PNG")
    ap.add_argument("--page-no", type=int, default=1)
    ap.add_argument("--out", default="", help="crops + result.json directory")
    ap.add_argument("--vlm", action="store_true",
                    help="also run Qwen2.5-VL on each article crop")
    args = ap.parse_args()
    if not os.path.isfile(args.page):
        print("not found:", args.page)
        return 1
    out = args.out or os.path.join(
        os.path.dirname(args.page) or ".",
        "seg_" + os.path.splitext(os.path.basename(args.page))[0])
    os.makedirs(out, exist_ok=True)
    res = pipeline.process_page(
        args.page, page_no=args.page_no, crops_dir=out,
        with_vlm=bool(args.vlm), debug=True)
    path = pipeline.dump_result(res, os.path.join(out, "result.json"))
    print(json.dumps({
        "status": res.status,
        "articles": len(res.articles),
        "errors": res.errors[:5],
        "result": path,
        "stages": [{"name": s.name, "status": s.status, "s": s.elapsed_s}
                   for s in res.stages],
    }, indent=2, ensure_ascii=False))
    return 0 if res.status in ("COMPLETED", "PARTIALLY_COMPLETED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
