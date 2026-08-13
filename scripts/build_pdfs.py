#!/usr/bin/env python3
"""
Build one PDF per newspaper: the full page first, then every clipping cut from
it, flowed underneath.

    python3 scripts/build_pdfs.py

Output goes to out/pdf/ as "<Paper> <YYYY-MM-DD>.pdf".

Note on text: PIL here has no complex-script shaping (raqm is unavailable), so
Devanagari drawn as text would come out as broken, unjoined glyphs. Every label
this script draws is therefore Latin only. The real headlines are never
redrawn -- they stay as printed pixels inside the clipping images, which is
both correct for every script and closer to the source anyway.
"""
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTDIR = os.path.join(ROOT, "out", "pdf")

DPI = 150
PAGE_W, PAGE_H = int(8.27 * DPI), int(11.69 * DPI)   # A4 portrait
MARGIN = 55
CONTENT_W = PAGE_W - 2 * MARGIN

INK = (19, 23, 32)
SLATE = (90, 100, 120)
FAINT = (139, 148, 167)
RULE = (214, 220, 230)
ACCENT = (27, 127, 212)

JOBS = [
    {"page": "out/pages/The_Tribune/Delhi_Edition/2026-08-12/p01.jpg",
     "regions": "out/regions_tribune_p01.json"},
    {"page": "out/pages/Jansatta/Delhi/2026-08-12/p03.jpg",
     "regions": "out/regions_jansatta_p03.json"},
]

FONT_DIRS = ["/System/Library/Fonts/Supplemental", "/System/Library/Fonts",
             "/Library/Fonts", "/usr/share/fonts/truetype/dejavu"]


def font(name_options, size):
    for d in FONT_DIRS:
        for n in name_options:
            p = os.path.join(d, n)
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except Exception:
                    continue
    return ImageFont.load_default()


F_TITLE = font(["Arial Bold.ttf", "Helvetica.ttc", "DejaVuSans-Bold.ttf"], 30)
F_SUB = font(["Arial.ttf", "Helvetica.ttc", "DejaVuSans.ttf"], 16)
F_LABEL = font(["Arial Bold.ttf", "DejaVuSans-Bold.ttf"], 11)
F_META = font(["Arial.ttf", "DejaVuSans.ttf"], 12)
F_HEAD = font(["Arial Bold.ttf", "DejaVuSans-Bold.ttf"], 15)
F_FOOT = font(["Arial.ttf", "DejaVuSans.ttf"], 10)


class Doc:
    """A4 page builder with a simple top-down flow and automatic page breaks."""

    def __init__(self, title, subtitle):
        self.title, self.subtitle = title, subtitle
        self.pages = []
        self._new()

    def _new(self):
        self.img = Image.new("RGB", (PAGE_W, PAGE_H), "white")
        self.d = ImageDraw.Draw(self.img)
        self.y = MARGIN
        self.pages.append(self.img)

    def _footer(self, img, n):
        d = ImageDraw.Draw(img)
        y = PAGE_H - MARGIN + 16
        d.line([(MARGIN, y), (PAGE_W - MARGIN, y)], fill=RULE, width=1)
        d.text((MARGIN, y + 8), self.subtitle, font=F_FOOT, fill=FAINT)
        t = "%d" % n
        w = d.textlength(t, font=F_FOOT)
        d.text((PAGE_W - MARGIN - w, y + 8), t, font=F_FOOT)

    def space(self, px):
        self.y += px

    def need(self, px):
        """Break to a new page if px would not fit."""
        if self.y + px > PAGE_H - MARGIN:
            self._new()
            return True
        return False

    def masthead(self):
        self.d.text((MARGIN, self.y), self.title, font=F_TITLE, fill=INK)
        self.y += 40
        self.d.text((MARGIN, self.y), self.subtitle, font=F_SUB, fill=SLATE)
        self.y += 26
        self.d.line([(MARGIN, self.y), (PAGE_W - MARGIN, self.y)], fill=INK, width=2)
        self.y += 22

    def section(self, label):
        self.need(46)
        self.d.text((MARGIN, self.y), label.upper(), font=F_LABEL, fill=ACCENT)
        self.y += 16
        self.d.line([(MARGIN, self.y), (PAGE_W - MARGIN, self.y)], fill=RULE, width=1)
        self.y += 14

    def caption(self, primary, meta):
        self.d.text((MARGIN, self.y), primary, font=F_HEAD, fill=INK)
        self.y += 20
        if meta:
            self.d.text((MARGIN, self.y), meta, font=F_META, fill=SLATE)
            self.y += 18

    def image(self, path, max_w=CONTENT_W, max_upscale=1.5, border=True):
        with Image.open(path) as im:
            im = im.convert("RGB")
            nat_w, nat_h = im.size
            target_w = min(max_w, int(nat_w * max_upscale))
            avail_h = PAGE_H - MARGIN - self.y
            scale = min(target_w / nat_w, avail_h / nat_h)
            # If it cannot fit on what is left, start a fresh page and refit.
            if scale < min(target_w / nat_w, 1.0) * 0.72:
                self._new()
                avail_h = PAGE_H - MARGIN - self.y
                scale = min(target_w / nat_w, avail_h / nat_h)
            w, h = max(1, int(nat_w * scale)), max(1, int(nat_h * scale))
            x = MARGIN + (CONTENT_W - w) // 2
            self.img.paste(im.resize((w, h), Image.LANCZOS), (x, self.y))
            if border:
                self.d.rectangle([x, self.y, x + w, self.y + h], outline=RULE, width=1)
            self.y += h

    def save(self, path):
        for i, p in enumerate(self.pages, 1):
            self._footer(p, i)
        self.pages[0].save(path, "PDF", save_all=True,
                           append_images=self.pages[1:], resolution=DPI)
        return len(self.pages)


def ascii_ok(s):
    """Latin text can be drawn; anything else stays in the image only."""
    try:
        s.encode("ascii")
        return bool(s.strip())
    except Exception:
        return False


def build(job):
    page = os.path.join(ROOT, job["page"])
    blob = json.load(open(os.path.join(ROOT, job["regions"]), encoding="utf-8"))
    paper, edition = blob["paper"], blob["edition"]
    date, pageno = blob["date"], blob["page"]
    arts = [r for r in blob["regions"] if r.get("type") == "article"]
    crops = os.path.join(os.path.dirname(page), "articles")

    with Image.open(page) as im:
        px = "%d x %d px" % im.size

    doc = Doc(paper, "%s  |  %s  |  page %s" % (edition, date, pageno))
    doc.masthead()

    doc.section("Full page as printed")
    doc.image(page, max_upscale=1.0)
    doc.space(10)

    n = 0
    for i, r in enumerate(arts, 1):
        path = os.path.join(crops, "p%s_a%02d.jpg" % (str(pageno).zfill(2), i))
        if not os.path.exists(path):
            continue
        n += 1
        with Image.open(path) as ci:
            cw, ch = ci.size

        bits = ["clipping %02d" % i]
        if r.get("columns"):
            bits.append("%s col" % r["columns"])
        if r.get("has_photo"):
            bits.append("photo")
        if r.get("continued_on_page"):
            bits.append("continued on page %s" % r["continued_on_page"])
        if r.get("package"):
            bits.append("umbrella package")
        bits.append("%d x %d px" % (cw, ch))
        meta = "  ·  ".join(bits)

        hl = r.get("headline_visible", "")
        primary = hl if ascii_ok(hl) else "Clipping %02d" % i

        # Keep caption and its image together on one page.
        est = 44 + int(min(CONTENT_W, cw * 1.5) * ch / max(cw, 1)) + 24
        if n == 1:
            doc.space(14)
            doc.section("Clippings (%d)" % len(arts))
        doc.need(min(est, PAGE_H - 2 * MARGIN))
        doc.caption(primary, meta)
        doc.image(path)
        doc.space(26)

    os.makedirs(OUTDIR, exist_ok=True)
    dest = os.path.join(OUTDIR, "%s %s.pdf" % (paper, date))
    pages = doc.save(dest)
    return dest, pages, n, px


def main():
    print("building PDFs -> %s\n" % os.path.relpath(OUTDIR, ROOT))
    for job in JOBS:
        dest, pages, n, px = build(job)
        print("  %-34s %2d pdf pages  %2d clippings  (source %s)"
              % (os.path.basename(dest), pages, n, px))
        print("     %.1f MB" % (os.path.getsize(dest) / 1e6))
    print("\nfolder: %s" % OUTDIR)


if __name__ == "__main__":
    sys.exit(main())
