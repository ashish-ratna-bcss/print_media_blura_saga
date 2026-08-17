#!/usr/bin/env python3
"""
Build the visual extraction report: full pages with detected regions drawn on
them, plus every clipping, emitted as base64 data URIs for a self-contained
HTML page.

    python3 scripts/build_report.py > out/report_assets.json
"""
import base64
import io
import json
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Region colours by type. Articles are the product; everything else is context.
COLORS = {"article": (34, 139, 230), "ad": (240, 140, 40),
          "masthead": (130, 130, 140), "index": (150, 90, 200)}

JOBS = [
    {"page": "out/pages/The_Tribune/Delhi_Edition/2026-08-12/p01.jpg",
     "regions": "out/regions_tribune_p01.json"},
    {"page": "out/pages/Jansatta/Delhi/2026-08-12/p03.jpg",
     "regions": "out/regions_jansatta_p03.json"},
]


def data_uri(img: Image.Image, max_edge: int, quality: int = 78) -> str:
    if max(img.size) > max_edge:
        r = max_edge / float(max(img.size))
        img = img.resize((max(1, int(img.width * r)), max(1, int(img.height * r))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def annotate(page_path: str, regions: list) -> Image.Image:
    """Draw the detected boxes over the page so boundaries are inspectable."""
    im = Image.open(page_path).convert("RGB")
    W, H = im.size
    lw = max(3, W // 400)
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    n = 0
    for r in regions:
        kind = r.get("type", "article")
        c = COLORS.get(kind, (120, 120, 120))
        x0, y0, x1, y1 = r["bbox"]
        box = [x0 * W, y0 * H, x1 * W, y1 * H]
        if kind == "article":
            n += 1
            d.rectangle(box, fill=c + (34,))
        d.rectangle(box, outline=c + (255,), width=lw)
        if kind == "article":
            # sequence badge, top-left of the region
            bx, by = box[0] + lw, box[1] + lw
            d.rectangle([bx, by, bx + lw * 13, by + lw * 13], fill=c + (255,))
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    return im


def main():
    out = {"pages": []}
    for job in JOBS:
        page = os.path.join(ROOT, job["page"])
        blob = json.load(open(os.path.join(ROOT, job["regions"]), encoding="utf-8"))
        regions = blob["regions"]
        src = Image.open(page)

        arts = [r for r in regions if r.get("type") == "article"]
        crops_dir = os.path.join(os.path.dirname(page), "articles")
        clips = []
        for i, r in enumerate(arts, 1):
            pth = os.path.join(crops_dir, "p%s_a%02d.jpg"
                               % (str(blob["page"]).zfill(2), i))
            if not os.path.exists(pth):
                continue
            with Image.open(pth) as ci:
                clips.append({
                    "seq": i,
                    "headline": r.get("headline_visible", ""),
                    "kicker": r.get("kicker_visible") or r.get("subheadline_visible") or "",
                    "columns": r.get("columns"),
                    "photo": bool(r.get("has_photo")),
                    "cont": r.get("continued_on_page"),
                    "package": bool(r.get("package")),
                    "px": "%d x %d" % ci.size,
                    "img": data_uri(ci, 620, 74),
                })

        out["pages"].append({
            "paper": blob["paper"], "edition": blob["edition"],
            "date": blob["date"], "page": blob["page"],
            "lang": blob.get("lang", "en"),
            "size": "%d x %d" % src.size,
            "n_article": len(arts),
            "n_ad": sum(1 for r in regions if r.get("type") == "ad"),
            "raw": data_uri(src, 760, 72),
            "annotated": data_uri(annotate(page, regions), 760, 72),
            "clips": clips,
        })
        src.close()

    dest = os.path.join(ROOT, "out/report_assets.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False)
    mb = os.path.getsize(dest) / 1e6
    print("wrote %s (%.1f MB)" % (dest, mb), file=sys.stderr)
    for p in out["pages"]:
        print("  %s p%s: %d articles, %d clips"
              % (p["paper"], p["page"], p["n_article"], len(p["clips"])), file=sys.stderr)


if __name__ == "__main__":
    main()
