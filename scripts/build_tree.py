#!/usr/bin/env python3
"""
Build the category tree over labels that already exist.

    python scripts/build_tree.py --dry-run     # show the placements it would make
    python scripts/build_tree.py               # place them, and move the articles
    python scripts/build_tree.py --show        # print the tree as it stands

Two levels: a small fixed set of headings (catdata.PARENTS) and free-form
labels beneath them. This is the pass that decides which heading each existing
label belongs under.

The reason it is cheap is that it works over LABELS, not articles. An archive
holds thousands of articles and tens of distinct labels -- 15 against 133 when
this was written -- so the parent of "otp fraud" is asked once and applies to
every article carrying it, and the ratio only improves as the archive grows.
Most labels never reach a model at all: catdata.SEED_PARENTS answers the
obvious ones for free.

Articles are then rewritten so that each carries BOTH its heading and its own
label. That denormalisation is what makes selecting a heading a single indexed
equality instead of expanding it into a list of children on every request.

Idempotent. A label already placed is left alone, so running this after each
night's collection costs one small model call for whatever is new.

Human corrections are never touched: an article whose category was set by hand
keeps it, exactly as a bulk relabel does.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import catdata, categories, classify, config                # noqa: E402
from epaper.corpus import Corpus                                        # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def show(store):
    for head in store.tree():
        if not head["total"] and not head["children"]:
            continue
        print("\n%-16s %4d" % (head["label"], head["total"]))
        for c in head["children"]:
            print("    %-24s %4d" % (c["label"], c["n"]))
    loose = store.unplaced()
    if loose:
        print("\nnot yet placed under any heading:")
        for r in loose:
            print("    %-24s %4d" % (r["label"], r["n"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="database name")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the placements, write nothing")
    ap.add_argument("--show", action="store_true",
                    help="print the tree and exit")
    args = ap.parse_args()

    config.load_env()
    c = Corpus(db=args.db, quiet=True)
    store = categories.CategoryStore(db=args.db) if args.db \
        else categories.store()
    if not store:
        print("cannot reach the category registry: %s" % categories.last_error())
        return 2

    if args.show:
        show(store)
        return 0

    # Rebuild first, so the counts this reasons about are the articles' own.
    store.rebuild(c.articles)

    loose = store.unplaced()
    if not loose:
        print("every label already sits under a heading")
        show(store)
        return 0

    print("%d label(s) with no heading yet:" % len(loose))
    for r in loose:
        print("   %-24s %4d articles" % (r["id"], r["n"]))

    labels = [r["id"] for r in loose]
    print("\nplacing %d label(s) -- seeded ones cost nothing, the rest are one"
          "\nmodel call per %d labels ..." % (len(labels), classify.PARENT_BATCH),
          flush=True)
    mapping = classify.assign_parents(labels)

    if not mapping:
        print("\nnothing could be placed. Is the model server running?")
        return 1

    print()
    for lab in labels:
        par = mapping.get(lab)
        print("   %-24s -> %s" % (lab, par or "(unplaced -- left as it is)"))

    if args.dry_run:
        print("\ndry run: nothing was written")
        return 0

    # Place the labels, then move the articles. Order matters only for
    # readability of a half-finished run: a placed label with unmoved articles
    # is recoverable by re-running, the reverse is confusing.
    placed = 0
    for lab, par in mapping.items():
        try:
            store.set_parent(lab, par)
            placed += 1
        except (KeyError, ValueError) as ex:
            print("   could not place %r: %s" % (lab, ex))

    moved = 0
    for lab, par in mapping.items():
        # One pass per label: the heading becomes the article's category, and
        # the label it used to carry becomes its subcategory -- unless it
        # already has one, which happens when the classifier gave both levels
        # and only the heading needed correcting.
        #
        # An aggregation pipeline rather than two updates, because both fields
        # are computed from the document as it was: a plain $set could not
        # read the old `category` while replacing it, and doing it in two
        # passes would leave a window where the label had been overwritten and
        # there was nothing left to copy into the child.
        #
        # Human-set categories are excluded. A person's answer outranks the
        # tree exactly as it outranks a relabel.
        moved += c.articles.update_many(
            {"category": lab, "category_source": {"$ne": "human"}},
            [{"$set": {
                "subcategory": {"$cond": [
                    {"$in": [{"$ifNull": ["$subcategory", ""]}, ["", None]]},
                    lab, "$subcategory"]},
                "category": par,
            }}]).modified_count

    store.rebuild(c.articles)
    print("\nplaced %d label(s), moved %d article(s)" % (placed, moved))
    show(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
