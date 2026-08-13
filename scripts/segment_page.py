#!/usr/bin/env python3
"""
Run article segmentation on a downloaded page.

    # full two-pass run (needs ANTHROPIC_API_KEY)
    python3 scripts/segment_page.py --page out/pages/.../p01.jpg

    # crop-only, from precomputed layout regions -- no API key needed.
    # Useful for eyeballing boundaries and for building ground truth.
    python3 scripts/segment_page.py --page ... --regions out/regions_tribune_p01.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from epaper import segment  # noqa: E402
from epaper.store import Store, article_id  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", required=True)
    ap.add_argument("--regions", default="", help="skip pass 1, use this layout")
    ap.add_argument("--no-text", action="store_true", help="crop only, skip pass 2")
    ap.add_argument("--paper", default="")
    ap.add_argument("--edition", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--page-no", type=int, default=1)
    args = ap.parse_args()

    page = os.path.abspath(args.page)
    if not os.path.exists(page):
        print("! no such page:", page)
        return 1

    meta = {"paper": args.paper, "edition": args.edition,
            "date": args.date, "page_no": args.page_no}
    if args.regions:
        blob = json.load(open(args.regions, encoding="utf-8"))
        regions = blob["regions"]
        for k in ("paper", "edition", "date", "page"):
            if blob.get(k):
                meta["page_no" if k == "page" else k] = blob[k]
        # detect_regions() attaches pixel boxes; do the same for a loaded file.
        from PIL import Image
        with Image.open(page) as im:
            W, H = im.size
        for r in regions:
            x0, y0, x1, y1 = r["bbox"]
            r["bbox_px"] = [int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)]
        print("layout: loaded %d regions from %s" % (len(regions), args.regions))
    else:
        try:
            regions = segment.detect_regions(page)
        except segment.NoApiKey as e:
            print("!", e)
            print("  (or pass --regions with a precomputed layout)")
            return 2
        print("layout: model found %d regions" % len(regions))

    crops_dir = os.path.join(os.path.dirname(page), "articles")
    kinds = {}
    for r in regions:
        kinds[r.get("type", "?")] = kinds.get(r.get("type", "?"), 0) + 1
    print("  types:", kinds)

    store = Store()
    have_mongo = store.ping()

    out, seq = [], 0
    for r in regions:
        if r.get("type") != "article":
            continue
        seq += 1
        crop = os.path.join(crops_dir, "p%s_a%02d.jpg"
                            % (str(meta["page_no"]).zfill(2), seq))
        segment.crop_region(page, r["bbox_px"], crop)
        rec = {
            "seq": seq,
            "headline": r.get("headline_visible"),
            "bbox": r.get("bbox"), "bbox_px": r["bbox_px"],
            "columns": r.get("columns"), "has_photo": r.get("has_photo"),
            "continued_on_page": r.get("continued_on_page"),
            "crop_image": crop,
            "text_source": "layout-only",
        }
        if not args.no_text and not args.regions:
            rec.update(segment.transcribe(crop))
            rec["text_source"] = "vlm"
        rec["confidence"] = segment.score(rec)
        out.append(rec)

        w = r["bbox_px"][2] - r["bbox_px"][0]
        h = r["bbox_px"][3] - r["bbox_px"][1]
        print("  a%02d %5dx%-5d cols=%-2s cont=%-4s %s"
              % (seq, w, h, r.get("columns"), r.get("continued_on_page"),
                 (r.get("headline_visible") or "")[:52]))

        if have_mongo and meta["paper"]:
            store.save_article({
                "article_id": article_id(meta["paper"], meta["date"],
                                         meta["page_no"], seq),
                **meta, **rec,
            })

    report = os.path.join(ROOT, "out", "articles_%s_p%s.json"
                          % ((meta["paper"] or "page").replace(" ", "_"),
                             str(meta["page_no"]).zfill(2)))
    json.dump({"meta": meta, "articles": out}, open(report, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    print("\n%d articles -> %s" % (len(out), os.path.relpath(report, ROOT)))
    print("crops -> %s" % os.path.relpath(crops_dir, ROOT))
    if have_mongo:
        print("mongo:", store.counts())
    return 0


if __name__ == "__main__":
    sys.exit(main())
