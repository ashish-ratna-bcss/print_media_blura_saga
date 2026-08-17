#!/usr/bin/env python3
"""Probe every Delhi e-paper host and write a capability report.

    python3 scripts/discover.py            # all papers
    python3 scripts/discover.py hindu jagran   # filter by name substring
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from epaper.probe import probe, save  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def main():
    args = list(sys.argv[1:])
    region = "delhi"
    for r in ("delhi", "gujarat"):
        if r in args:
            region = r
            args.remove(r)
    cfg = json.load(open(os.path.join(ROOT, "data/%s_papers.json" % region),
                         encoding="utf-8"))
    papers = cfg["papers"]
    if args:
        needles = [a.lower() for a in args]
        papers = [p for p in papers
                  if any(n in p["name"].lower() or n in p["host"].lower() for n in needles)]

    print("Probing %d %s papers\n" % (len(papers), region))
    print("%-24s %-40s %-13s %5s %4s  %s"
          % ("PAPER", "HOST", "PLATFORM", "HTTP", "API", "VERDICT"))
    print("-" * 110)

    results = []
    for p in papers:
        r = probe(p["host"], p["name"])
        # Fall back to alternate hosts if the primary is dead.
        for alt in p.get("alt", []):
            if r.reachable:
                break
            r = probe(alt, p["name"])
        r.notes = (r.notes + " lang=%s group=%s" % (p["lang"], p["group"])).strip()
        results.append(r)
        print("%-24s %-40s %-13s %5s %4d  %s"
              % (p["name"][:24], r.host[:40], r.platform,
                 r.http_status, len(r.working_api), r.verdict()),
              flush=True)

    out = os.path.join(ROOT, "out/%s_probe.json" % region)
    save(results, out)

    print("\n" + "=" * 110)
    buckets = {}
    for r in results:
        buckets.setdefault(r.verdict(), []).append(r)
    for v in ("api", "known-platform", "cv", "unreachable"):
        rs = buckets.get(v, [])
        print("%-16s %2d   %s" % (v, len(rs), ", ".join(x.name or x.host for x in rs)))
    print("\nplatforms:", json.dumps(
        {k: sum(1 for r in results if r.platform == k)
         for k in sorted({r.platform for r in results})}, indent=None))
    print("saved ->", out)


if __name__ == "__main__":
    main()
