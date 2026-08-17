#!/usr/bin/env python3
"""
Try a beat vocabulary against real copy, with nothing else running.

    python3 scripts/topic_check.py --demo
    python3 scripts/topic_check.py --json out/articles.json
    python3 scripts/topic_check.py --text stories.txt --topic "Delhi Police"
    cat story.txt | python3 scripts/topic_check.py

No layout model, no VLM, no network, no database -- this only exercises
epaper/keywords.py, so it runs anywhere Python does. That is the point: the term
list is the part that needs a human eye on real Hindi and English copy, and
waiting for a GPU box to look at it is a bad trade.

What to look for:

  DROP rows   are what the prefilter would hide from the model entirely. A real
              story here is a hole in the vocabulary -- add the term it should
              have matched.
  weak CORE   a story carried only by CONTEXT hits (placenames, courts) is a
              near-miss. It survives the prefilter but should not be scoring
              well, and if it does the weights need attention.
  passing     "the minister said police were informed" should land under the
              threshold. If passing mentions clear it, FULL_SCORE is too low.

Scores here are the lexical layer alone. In hybrid mode the model still judges
relevance afterwards, so a generous score is not by itself a bug -- a generous
DROP is.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from epaper import keywords  # noqa: E402

# Devanagari has to survive being piped to a file, where Windows would otherwise
# pick a locale codec that cannot represent it.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEMO = [
    ("Police arrest two in robbery case",
     "Delhi Police arrested two suspects on Tuesday. An FIR was registered at "
     "Dwarka police station and the accused were sent to custody, DCP said."),
    ("पुलिस ने दो बदमाशों को दबोचा",
     "दिल्ली पुलिस ने थाने में प्राथमिकी दर्ज कर दो आरोपियों को गिरफ्तार किया। "
     "एसएचओ ने बताया कि गिरोह लूट की वारदात में शामिल था।"),
    ("थाने में तैनात सिपाही निलंबित",
     "करोल बाग थाने में तैनात एक सिपाही को निलंबित कर दिया गया।"),
    ("Marathon draws 20,000 runners",
     "The annual marathon drew a record crowd. Organisers said police were "
     "informed in advance and the route was cleared by 9am."),
    ("Sensex climbs 400 points",
     "Equity benchmarks ended higher as banking stocks gained and the firm "
     "reported profits for the first quarter of this year."),
    ("Fir trees planted in city park",
     "The horticulture department planted fir saplings. It was the first such "
     "drive this year and the firm handling it was paid promptly."),
]

TIERS = ((keywords.CORE, "CORE"), (keywords.RELATED, "RELATED"),
         (keywords.CONTEXT, "CONTEXT"))


def load_json(path):
    """Accept a bare list, or the {"articles": [...]} / {"matches": [...]} wrappers."""
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, dict):
        d = d.get("articles") or d.get("matches") or d.get("results") or []
    if not isinstance(d, list):
        raise SystemExit("%s: expected a list of articles" % path)
    return [x for x in d if isinstance(x, dict)]


def load_text(fh):
    """Blocks split on a --- line or a blank line. First line is the headline."""
    blocks, cur = [], []
    for raw in fh.read().splitlines():
        if raw.strip() == "---" or (not raw.strip() and cur):
            blocks.append(cur)
            cur = []
            continue
        if raw.strip():
            cur.append(raw)
    if cur:
        blocks.append(cur)
    return [{"headline": b[0], "body": "\n".join(b[1:])} for b in blocks if b]


def tiers_of(hits, weight_of):
    """Group matched terms by weight, so a CONTEXT-only match is obvious."""
    out = []
    for w, name in TIERS:
        got = [t for t in hits if weight_of.get(t) == w]
        if got:
            out.append((name, got))
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Score articles against a beat vocabulary. No model needed.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--json", help="JSON file: list of {headline, body} dicts")
    src.add_argument("--text", help="text file; blocks split on --- or a blank line")
    src.add_argument("--demo", action="store_true", help="built-in EN/HI examples")
    ap.add_argument("--topic", default="Delhi Police")
    ap.add_argument("--min-score", type=float, default=0.5)
    ap.add_argument("--terms", action="store_true", help="print the vocabulary and exit")
    ap.add_argument("--quiet", action="store_true", help="table only, no per-story detail")
    a = ap.parse_args()

    spec = keywords.expand(a.topic)
    weight_of = {t: w for w, _, t in spec["patterns"]}

    print("\ntopic   %r" % a.topic)
    if spec["expanded"]:
        print("beat    %s  (%d terms)" % (spec["beat"], len(spec["terms"])))
    else:
        print("beat    none -- searched as written, no expansion, nothing prefiltered")

    if a.terms:
        for w, name in TIERS:
            got = sorted(t for t, tw in weight_of.items() if tw == w)
            print("\n%s  %.1f  (%d)" % (name, w, len(got)))
            print("  " + ", ".join(got))
        return 0

    if a.demo:
        arts = [{"headline": h, "body": b} for h, b in DEMO]
    elif a.json:
        arts = load_json(a.json)
    elif a.text:
        with open(a.text, encoding="utf-8") as f:
            arts = load_text(f)
    elif not sys.stdin.isatty():
        arts = load_text(sys.stdin)
    else:
        ap.error("give --demo, --json, --text, or pipe text on stdin")

    if not arts:
        raise SystemExit("no articles to score")

    pf = keywords.prefilter(arts, spec)
    kept = set(pf["keep"])

    print("stories %d\n" % len(arts))
    print("  %-4s %-6s %-6s %s" % ("", "score", "state", "headline"))
    print("  " + "-" * 74)
    rows = []
    for i, (art, s) in enumerate(zip(arts, pf["scores"])):
        hl = (art.get("headline") or "(no headline)").replace("\n", " ")
        if i not in kept:
            state = "DROP"
        elif s["score"] >= a.min_score:
            state = "MATCH"
        elif not s["testable"]:
            state = "unread"
        else:
            state = "weak"
        print("  %-4d %-6.2f %-6s %s" % (i + 1, s["score"], state, hl[:58]))
        rows.append((i + 1, hl, s, state))

    if not a.quiet:
        for n, hl, s, state in rows:
            print("\n%d. %s" % (n, hl[:70]))
            print("   %s   score %.2f   weight %.2f%s"
                  % (state, s["score"], s["weight"],
                     "   headline hit" if s["headline_hit"] else ""))
            if not s["testable"]:
                print("   too little text to judge -- kept, never dropped on absence")
            for name, got in tiers_of(s["hits"], weight_of):
                print("   %-8s %s" % (name, ", ".join(got)))
            if not s["hits"]:
                print("   no terms matched")

    n_match = sum(1 for _, _, s, st in rows if st == "MATCH")
    print("\n" + "=" * 78)
    print("%d scored   %d would match at >=%.2f   %d dropped before any model call"
          % (len(arts), n_match, a.min_score, pf["dropped"]))
    print("prefilter: %s" % pf["reason"])
    if pf["dropped"]:
        print("\ncheck the DROP rows: a real story there is a missing term.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
