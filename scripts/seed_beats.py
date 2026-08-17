#!/usr/bin/env python3
"""
Import every state and union territory police beat into MongoDB.

    python scripts/seed_beats.py                # add what is missing
    python scripts/seed_beats.py --force        # also restore edited beats
    python scripts/seed_beats.py --list         # show what is stored, no writes
    python scripts/seed_beats.py --check "Punjab Police"

Safe to re-run. An existing beat is left alone unless --force, because a
redeploy quietly reverting terms someone added from the backend is a worse
failure than a stale seed.

The web app seeds an empty database by itself at startup, so this script is
for re-seeding, inspecting, and restoring after an edit went wrong.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import beatdata, beats as beats_mod, config, keywords  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite beats that already exist")
    ap.add_argument("--list", action="store_true", help="show stored beats only")
    ap.add_argument("--check", metavar="QUERY",
                    help="show which beat a query resolves to, and why")
    a = ap.parse_args()

    config.load_env()
    print("host      %s" % config.hosts(config.mongo_uri()))
    print("database  %s" % config.mongo_db())
    print("built-in  %d beats (%d states, %d union territories)"
          % (len(beatdata.STATES),
             sum(1 for s in beatdata.STATES if s.get("kind") == "state"),
             sum(1 for s in beatdata.STATES if s.get("kind") == "ut")))

    store = beats_mod.store()
    if not store:
        sys.exit("\nmongodb unavailable: %s\n"
                 "The built-in vocabulary still works -- run "
                 "scripts/topic_check.py to use it without a database."
                 % beats_mod.last_error())

    if a.check:
        return check(store, a.check)
    if a.list:
        return show(store)

    print()
    counts = store.seed(force=a.force)
    print("packs     %d" % counts["packs"])
    print("added     %d" % counts["added"])
    print("restored  %d" % counts["restored"])
    print("kept      %d   (already present; --force to overwrite)"
          % counts["kept"])

    n = store.load_into_keywords()
    print("\nactive    %d beats loaded from mongodb" % n)
    show(store, brief=True)
    print("\nStored articles keep the scores they were written with. To apply\n"
          "this vocabulary to what is already indexed:\n"
          "   curl -X POST localhost:8080/api/corpus/rescore")
    return 0


def show(store, brief: bool = False):
    rows = store.raw()
    if not rows:
        print("\nnothing stored yet -- run without --list to seed")
        return 0
    on = [r for r in rows if r.get("enabled", True)]
    print("\n%d stored, %d enabled" % (len(rows), len(on)))
    if brief:
        return 0
    packs = {p["id"]: p for p in store.packs.find({}, {"_id": 0})}
    for pid, p in packs.items():
        n = sum(len(g.get("terms") or []) for g in (p.get("terms") or []))
        na = sum(len(g.get("terms") or []) for g in (p.get("acronyms") or []))
        print("\npack %-12s %d terms, %d acronyms  (shared by every beat "
              "that references it)" % (pid, n, na))
    print()
    for r in sorted(rows, key=lambda x: (x.get("region_kind") != "ut",
                                         x.get("label", ""))):
        own = sum(len(g.get("terms") or []) for g in (r.get("terms") or []))
        print("  %-1s %-34s %-3s %3d places%s"
              % ("" if r.get("enabled", True) else "x",
                 r.get("label"), r.get("state_code", ""),
                 len(r.get("region") or []),
                 "  +%d own terms" % own if own else ""))
    return 0


def check(store, query: str):
    """Resolve a query against what is actually loaded, and explain the result."""
    store.load_into_keywords()
    beat = keywords.resolve(query)
    print("\nquery     %r" % query)
    if not beat:
        print("resolves  (nothing) -- searched as written, no expansion")
        return 0
    spec = keywords.expand(query)
    print("resolves  %s  (%s)" % (beat["id"], beat.get("label")))
    print("terms     %d" % len(spec["terms"]))
    print("region    %d places, %d elsewhere"
          % (len(beat.get("region") or []), len(beat.get("region_out") or [])))
    print("sample    %s" % ", ".join(spec["terms"][:14]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
