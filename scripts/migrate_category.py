#!/usr/bin/env python3
"""
Move the beat id out of `category` and into `primary_beat`.

    python scripts/migrate_category.py --dry-run     # count, change nothing
    python scripts/migrate_category.py               # migrate
    python scripts/migrate_category.py --db epaper_corpus_test

`category` used to hold whichever beat scored highest -- "delhi-police",
"punjab-police" -- because with one beat the distinction between where a story
belongs and what it is about did not matter. With 36 it does, and the field a
topical browse would key on was already full of geography.

So the value moves to `primary_beat`, which is what it always meant, and
`category` is emptied to await the classifier. Nothing is computed here: the
beat ids being moved are the ones already stored, so this cannot disagree with
the vocabulary that produced them. Re-running the scorer is a separate concern
and a separate command (POST /api/corpus/rescore).

Idempotent, and safe to interrupt. A document is selected only when it still
carries a beat id under `category`, so a second run finds nothing to do and a
run that stopped halfway resumes exactly where it was.

The one thing it will not do is guess. A `category` value that is not a known
beat id is left untouched and reported, because the only way that can happen
is that something else wrote it -- and silently blanking a field whose origin
we do not understand is how data goes missing.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient                                    # noqa: E402

from epaper import config, keywords                                # noqa: E402

# Windows defaults stdout to cp1252, which cannot encode Devanagari. Printing
# a Hindi paper name or headline would raise UnicodeEncodeError and kill the
# run -- the same defaulting that once broke every Hindi page upload.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="database name (default: whatever .env says)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args()

    config.load_env()
    uri = config.mongo_uri()
    name = args.db or config.mongo_db()

    print("database   %s / %s" % (config.hosts(uri), name))
    print("mode       %s" % ("dry run -- no writes" if args.dry_run else "migrating"))

    db = MongoClient(uri, **config.mongo_kwargs())[name]
    articles = db.articles

    total = articles.count_documents({})
    if not total:
        print("\nnothing to do: the articles collection is empty")
        return 0

    # Every beat id the vocabulary currently knows about. Loaded from the
    # built-ins rather than MongoDB on purpose -- this must work on a machine
    # that cannot reach the beats collection, and the built-in set is the
    # floor that every stored beat was seeded from.
    known = set(keywords.BEATS)

    # Already migrated, still to do, and the ones we refuse to touch.
    done = articles.count_documents({"primary_beat": {"$exists": True}})
    pending = list(articles.find(
        {"category": {"$in": sorted(known)}},
        {"_id": 1, "category": 1, "primary_beat": 1}))

    # A non-empty `category` that is not a beat id. Either the classifier has
    # already run here, or something wrote a value this script does not
    # recognise. Both are reasons to leave it alone.
    unknown = [d["category"] for d in articles.find(
        {"category": {"$nin": sorted(known) + ["", None]}},
        {"category": 1}).limit(200)]

    print("\narticles           %6d" % total)
    print("carrying a beat id %6d   <- to migrate" % len(pending))
    print("already migrated   %6d" % done)
    if unknown:
        seen = sorted(set(unknown))
        print("unrecognised       %6d   <- left untouched: %s"
              % (len(unknown), ", ".join(repr(u) for u in seen[:5])
                 + (" ..." if len(seen) > 5 else "")))

    if not pending:
        print("\nnothing to migrate")
        return 0

    if args.dry_run:
        print("\ndry run: no documents were written")
        return 0

    moved = skipped = 0
    for d in pending:
        beat = d["category"]
        # A document that already has a primary_beat disagreeing with the
        # value being moved means this ran before against a different
        # vocabulary. Keep what is there and say so, rather than overwriting
        # a good value with an older one.
        existing = d.get("primary_beat")
        if existing and existing != beat:
            skipped += 1
            print("   kept %s: primary_beat=%r, category=%r"
                  % (d["_id"], existing, beat))
            continue
        articles.update_one({"_id": d["_id"]},
                            {"$set": {"primary_beat": beat, "category": ""}})
        moved += 1

    print("\nmoved      %6d   category -> primary_beat, category emptied" % moved)
    if skipped:
        print("kept       %6d   already had a different primary_beat" % skipped)

    left = articles.count_documents({"category": {"$in": sorted(known)}})
    print("remaining  %6d   %s" % (left, "clean" if left == 0 else "RE-RUN NEEDED"))

    print("\nNext: articles now have an empty `category`, waiting for the")
    print("classifier. Beat search and scoring are unaffected -- they read")
    print("`beats` and `beat_scores`, which this did not touch.")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
