#!/usr/bin/env python3
"""
Download today's Delhi editions at full resolution and record them in MongoDB.

    python3 scripts/fetch_delhi.py                 # today, all Readwhere papers
    python3 scripts/fetch_delhi.py --date 2026-08-11
    python3 scripts/fetch_delhi.py --paper tribune --max-pages 4
"""
import argparse
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from epaper.acquire import image_size, page_path, stitch_tiles  # noqa: E402
from epaper.platforms.readwhere import Readwhere  # noqa: E402
from epaper.store import Store  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT = os.path.join(ROOT, "out")

# Readwhere-backed Delhi papers found by scripts/discover.py
PAPERS = [
    {"paper": "The Tribune",       "host": "epaper.tribuneindia.com",        "lang": "en"},
    {"paper": "Dainik Tribune",    "host": "epaper.dainiktribuneonline.com", "lang": "hi"},
    {"paper": "Jansatta",          "host": "epaper.jansatta.com",            "lang": "hi"},
    {"paper": "Financial Express", "host": "epaper.financialexpress.com",    "lang": "en"},
    {"paper": "The Statesman",     "host": "epaper.thestatesman.com",        "lang": "en"},
]

# An edition counts as Delhi if its name matches this.
DELHI = re.compile(r"delhi|ncr|national", re.I)


def pick_editions(rw: Readwhere) -> list:
    titles = rw.titles()
    delhi = [t for t in titles if DELHI.search(t["name"])]
    # Fall back to the flagship title so a paper without a named Delhi edition
    # still yields pages rather than silently producing nothing.
    return delhi or titles[:1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--paper", default="")
    ap.add_argument("--max-pages", type=int, default=0, help="0 = all pages")
    args = ap.parse_args()

    store = Store()
    if not store.ping():
        print("! mongodb not reachable at localhost:27017")
        return 1
    print("mongodb ok | date %s\n" % args.date)

    papers = [p for p in PAPERS
              if not args.paper or args.paper.lower() in p["paper"].lower()
              or args.paper.lower() in p["host"]]

    grand = 0
    for p in papers:
        print("=" * 78)
        print("%s  (%s)" % (p["paper"], p["host"]))
        rw = Readwhere(p["host"])
        try:
            editions = pick_editions(rw)
        except Exception as e:
            print("  ! title discovery failed: %s" % e)
            continue
        if not editions:
            print("  ! no editions found")
            continue

        store.save_paper({"host": p["host"], "name": p["paper"], "lang": p["lang"],
                          "platform": "readwhere", "city": "Delhi",
                          "editions": editions})

        for ed in editions:
            issue = rw.issue_for_date(ed["id"], args.date)
            if not issue:
                print("  %-22s no issue published on %s" % (ed["name"][:22], args.date))
                continue
            cfg = rw.issue_config(issue)
            n = cfg.get("numPages", 0)
            print("  %-22s issue=%s pages=%s" % (ed["name"][:22], issue, n))
            if not n:
                continue

            meta = rw.all_page_meta(issue, n)
            limit = args.max_pages or n
            got = 0
            for pno in range(1, min(limit, n) + 1):
                pm = meta.get(str(pno)) or meta.get(pno)
                if not pm:
                    continue
                level = rw.best_level(pm)
                tiles = rw.tiles(level)
                dest = page_path(OUT, p["paper"], ed["name"], args.date, pno)
                if not os.path.exists(dest):
                    if not stitch_tiles(tiles, level["width"], level["height"], dest):
                        print("      p%-3d FAILED" % pno)
                        continue
                w, h = image_size(dest)
                store.save_page({
                    "paper": p["paper"], "host": p["host"], "edition": ed["name"],
                    "edition_id": ed["id"], "date": args.date, "page_no": pno,
                    "issue_id": issue, "page_key": pm.get("key"),
                    "image_path": dest, "width": w, "height": h,
                    "platform": "readwhere", "lang": p["lang"],
                    "has_publisher_articles": False,  # Readwhere: none available
                    "segmented": False,
                })
                got += 1
                print("      p%-3d %sx%s  %s" % (pno, w, h, os.path.relpath(dest, ROOT)))
            grand += got
            print("    -> %d pages stored" % got)

    print("\n" + "=" * 78)
    print("pages downloaded this run: %d" % grand)
    print("mongo:", store.counts())
    return 0


if __name__ == "__main__":
    sys.exit(main())
