#!/usr/bin/env python3
"""
Build one PDF per newspaper covering EVERY downloaded page.

Uses DocLayout-YOLO + Shapely grouping (+ optional Qwen2.5-VL) only.

    python3 scripts/build_all_pdfs.py
    python3 scripts/build_all_pdfs.py --paper jansatta --max-pages 4
"""
import argparse
import glob
import json
import os
import sys

from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from epaper import local_backend  # noqa: E402
from epaper.crop import crop_region  # noqa: E402
from epaper.store import Store, article_id  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
from build_pdfs import Doc, ascii_ok, CONTENT_W, PAGE_H, MARGIN, OUTDIR  # noqa: E402


def manual_regions_for(paper, page_no):
    """Pick up a hand-checked layout file if one exists for this page."""
    for f in glob.glob(os.path.join(ROOT, "out", "regions_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                b = json.load(fh)
        except Exception:
            continue
        if b.get("paper") == paper and int(b.get("page", -1)) == int(page_no):
            for r in b["regions"]:
                r.setdefault("detector", "manual")
            return b["regions"]
    return None


def attach_px(regions, size):
    W, H = size
    for r in regions:
        if "bbox_px" not in r:
            x0, y0, x1, y1 = r["bbox"]
            r["bbox_px"] = [int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)]
    return regions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", default="")
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--no-clips", action="store_true", help="full pages only")
    ap.add_argument("--no-text", action="store_true",
                    help="group and crop, but skip VLM transcription")
    args = ap.parse_args()

    store = Store()
    if not store.ping():
        print("! mongodb unreachable")
        return 1

    h = local_backend.health()
    if not h.get("ready"):
        print("! local backend not ready. Run scripts/health.py for detail:")
        print(json.dumps(h, indent=1))
        return 2
    print("backend ready: %s | %s\n"
          % (os.path.basename(h.get("weights_path") or ""),
             (h.get("vlm") or {}).get("model")))

    papers = sorted({d["paper"] for d in store.db.pages.find({}, {"paper": 1})})
    if args.paper:
        papers = [p for p in papers if args.paper.lower() in p.lower()]

    os.makedirs(OUTDIR, exist_ok=True)
    print("building %d PDF(s) -> %s\n" % (len(papers), os.path.relpath(OUTDIR, ROOT)))

    for paper in papers:
        pages = list(store.db.pages.find({"paper": paper}).sort(
            [("edition", 1), ("page_no", 1)]))
        if args.max_pages:
            pages = pages[:args.max_pages]
        if not pages:
            continue
        date = pages[0]["date"]

        doc = Doc(paper, "%s  |  %d pages  |  Delhi" % (date, len(pages)))
        doc.masthead()

        n_clip = 0
        for pi, pg in enumerate(pages, 1):
            path = pg["image_path"]
            if not os.path.exists(path):
                continue
            with Image.open(path) as im:
                size = im.size

            doc.need(240)
            doc.section("%s  ·  page %s  ·  full page as printed"
                        % (pg["edition"], pg["page_no"]))
            doc.image(path, max_upscale=1.0)
            doc.space(16)

            if args.no_clips:
                print("  %-18s p%-3s full page" % (paper[:18], pg["page_no"]), flush=True)
                continue

            regions = manual_regions_for(paper, pg["page_no"])
            src = "manual"
            if regions is None:
                regions = local_backend.detect_regions(
                    path, pg["page_no"], paper=paper, date=pg["date"])
                src = "local"
            regions = attach_px(regions, size)
            arts = [r for r in regions if r.get("type") == "article"]

            page_articles = []
            crops_dir = os.path.join(os.path.dirname(path),
                                     "articles" if src == "manual" else "articles_local")
            placed = 0
            for ai, r in enumerate(arts, 1):
                crop = os.path.join(
                    crops_dir, "p%s_a%02d.jpg" % (str(pg["page_no"]).zfill(2), ai))
                try:
                    crop_region(path, r["bbox_px"], crop)
                except Exception:
                    continue
                with Image.open(crop) as ci:
                    cw, ch = ci.size
                if cw < 120 or ch < 90:
                    continue

                bits = ["clipping %02d of page %s" % (ai, pg["page_no"])]
                if r.get("columns"):
                    bits.append("%s col" % r["columns"])
                if r.get("continued_on_page"):
                    bits.append("continued on page %s" % r["continued_on_page"])
                bits.append("%d x %d px" % (cw, ch))
                bits.append({"local": "DocLayout-YOLO + Shapely + IndicOCR + Qwen2.5-VL",
                             "manual": "verified layout"}.get(src, "local"))
                hl = r.get("headline_visible", "")
                primary = hl if ascii_ok(hl) else "Clipping %02d" % ai

                if placed == 0:
                    doc.need(60)
                    doc.section("clippings from page %s (%d)"
                                % (pg["page_no"], len(arts)))
                est = 44 + int(min(CONTENT_W, cw * 1.5) * ch / max(cw, 1)) + 26
                doc.need(min(est, PAGE_H - 2 * MARGIN))
                doc.caption(primary, "  ·  ".join(bits))
                doc.image(crop)
                doc.space(22)
                placed += 1

                rec = {
                    "article_id": article_id(paper, pg["date"], pg["page_no"], ai),
                    "paper": paper, "edition": pg["edition"], "date": pg["date"],
                    "page_no": pg["page_no"], "seq": ai,
                    "headline": hl or None, "bbox": r["bbox"], "bbox_px": r["bbox_px"],
                    "crop_image": crop, "lang": pg.get("lang"),
                    "elements": r.get("elements"), "has_photo": r.get("has_photo"),
                    "package": r.get("package"),
                    "grouping_confidence": r.get("grouping_confidence"),
                    "text_source": "layout-only", "detector": r.get("detector", src),
                }
                if not args.no_text:
                    try:
                        fields = local_backend.transcribe(crop)
                        rec.update(fields)
                        rec["text_source"] = "vlm"
                    except Exception as ex:
                        rec["transcribe_error"] = str(ex)[:200]
                        print("      ! read failed on a%02d: %s" % (ai, str(ex)[:90]),
                              flush=True)
                store.save_article(rec)
                page_articles.append(rec)
            if page_articles:
                dd = local_backend.debug_dir_for(paper, pg["date"], pg["page_no"])
                os.makedirs(dd, exist_ok=True)
                with open(os.path.join(dd, "articles.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(page_articles, fh, ensure_ascii=False,
                              indent=1, default=str)
            n_clip += placed
            store.db.pages.update_one({"_id": pg["_id"]}, {"$set": {"segmented": True}})
            print("  %-18s p%-3s %2d clippings (%s)  [%d/%d]"
                  % (paper[:18], pg["page_no"], placed, src, pi, len(pages)), flush=True)
            doc.space(10)

        dest = os.path.join(OUTDIR, "%s %s.pdf" % (paper, date))
        npdf = doc.save(dest)
        print("  -> %-38s %3d pdf pages, %3d clippings, %.1f MB\n"
              % (os.path.basename(dest), npdf, n_clip,
                 os.path.getsize(dest) / 1e6), flush=True)

    print("folder:", OUTDIR)
    print("mongo:", store.counts())
    return 0


if __name__ == "__main__":
    sys.exit(main())
