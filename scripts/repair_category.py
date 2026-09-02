"""Repair the rows the old server wrote a beat id into `category`.

The old code stored WHERE a story belongs (a police-force id) in the field
that now means WHAT it is about. `raw_category` kept the model's own wording,
so the topic is recoverable for every row that still has one.
"""
import sys

for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8")
    except Exception:
        pass
sys.path.insert(0, r"C:\Users\sunakshi\e-paper")

from epaper import catdata, categories, corpus, keywords   # noqa: E402

APPLY = "--apply" in sys.argv
col = corpus.Corpus().articles
beats = sorted(set(keywords.BEATS))

rows = list(col.find({"category": {"$in": beats}},
                     {"uid": 1, "headline": 1, "category": 1,
                      "raw_category": 1, "subcategory": 1,
                      "primary_beat": 1}))

fixed = stuck = 0
print("%-6s %-16s %-14s %s" % ("", "category ->", "subcategory", "headline"))
for d in rows:
    beat = d["category"]                       # what the old code put there
    raw = (d.get("raw_category") or "").strip()
    if not raw:
        stuck += 1
        print("%-6s %-16s %-14s %s" % ("SKIP", "(no raw_category)", "",
                                       (d.get("headline") or "")[:40]))
        continue

    parent = catdata.normalise(raw)
    child = catdata.normalise(d.get("subcategory") or "")
    if parent and not catdata.is_parent(parent):
        child = child or parent
        parent = catdata.seed_parent(parent) or ""
    if child == parent:
        child = ""
    parent = parent or catdata.UNKNOWN

    up = {"category": parent, "subcategory": child}
    # The beat id was never wrong -- only in the wrong field.
    if not (d.get("primary_beat") or "").strip():
        up["primary_beat"] = beat

    if APPLY:
        col.update_one({"_id": d["_id"]}, {"$set": up})
    fixed += 1
    print("%-6s %-16s %-14s %s"
          % ("fix", parent, child or "-", (d.get("headline") or "")[:40]))

print("\n%s  %d repaired, %d without a raw_category"
      % ("APPLIED" if APPLY else "DRY RUN (nothing written)", fixed, stuck))

if APPLY:
    try:
        categories.store().rebuild(col)
        print("category registry rebuilt")
    except Exception as ex:
        print("registry rebuild failed: %s" % ex)
