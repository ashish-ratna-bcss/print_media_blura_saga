#!/usr/bin/env python3
"""
Measure how often the assigned topic is the right one.

    python scripts/classify_check.py --sample 60 > sample.json   # draw a set
    <edit sample.json: put the correct topic in "expected">
    python scripts/classify_check.py --score sample.json         # grade it
    python scripts/classify_check.py --stability 20              # no labels needed

Three modes, because two different questions get confused with each other.

  --sample     Draws a random set of articles and writes them out with an
               empty "expected" field for a person to fill in. Random rather
               than the most recent, because a day's paper is not a sample of
               a newspaper -- one big story skews a whole edition.

  --score      Grades the stored labels against what a person wrote. This is
               ACCURACY, and it is the only mode that can tell you the labels
               are right. It needs human judgement and cannot be automated.

  --stability  Re-labels the same articles twice and reports how often the
               two runs agree. This is CONSISTENCY, needs no human input, and
               is worth running first: a model that contradicts itself cannot
               be accurate, so a poor stability score saves the afternoon that
               labelling a sample by hand would have cost.

Accuracy is scored two ways, and the gap between them is the interesting part:

  exact       the stored label equals the expected one
  aliased     they normalise to the same entry -- "cyber crime" vs "cybercrime"

A large gap means the model is right about the subject and inconsistent about
the wording, which is a normalisation problem and cheap to fix. A low aliased
score means it is wrong about the subject, which is a model or prompt problem
and is not.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import catdata, classify, config                      # noqa: E402
from epaper.corpus import Corpus                                  # noqa: E402

# Windows defaults stdout to cp1252, which cannot encode Devanagari -- so
# printing a Hindi headline raises UnicodeEncodeError and kills the run after
# the measurement has already been paid for. The same defaulting is what broke
# every Hindi page upload once (see run_job's note on encoding="utf-8"). A
# reporting tool for a bilingual corpus has to say this out loud.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def draw(c, n: int, seed: int) -> list:
    """A random sample of labelled articles, written out for grading."""
    rows = list(c.articles.aggregate([
        {"$match": {"category": {"$nin": ["", None]},
                    "body": {"$nin": ["", None]}}},
        {"$sample": {"size": int(n)}}]))
    random.Random(seed).shuffle(rows)
    return [{
        "uid": r.get("uid"),
        "headline": r.get("headline", ""),
        "opening": (r.get("body") or "")[:300],
        "assigned": r.get("category", ""),
        "raw": r.get("raw_category", ""),
        # The field a person fills in. Left empty rather than pre-filled with
        # the assigned label: seeing the model's answer first is how a grader
        # talks themselves into agreeing with it.
        "expected": "",
    } for r in rows]


def score(path: str) -> int:
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)

    graded = [r for r in rows if (r.get("expected") or "").strip()]
    if not graded:
        print("no rows have an \"expected\" value yet.")
        print("Fill that field in for each article, then re-run --score.")
        return 2

    exact = aliased = 0
    per_topic = {}
    wrong = []
    for r in graded:
        want_raw = r["expected"].strip()
        want = catdata.normalise(want_raw) or want_raw
        got = (r.get("assigned") or "").strip()
        ok_exact = got == want_raw
        ok_alias = got == want
        exact += ok_exact
        aliased += ok_alias
        t = per_topic.setdefault(want, {"n": 0, "ok": 0})
        t["n"] += 1
        t["ok"] += ok_alias
        if not ok_alias:
            wrong.append((want, got, r.get("headline", "")[:52]))

    n = len(graded)
    print("graded          %d of %d rows" % (n, len(rows)))
    print("exact match     %3d  %5.1f%%" % (exact, 100.0 * exact / n))
    print("after aliasing  %3d  %5.1f%%   <- the number that matters"
          % (aliased, 100.0 * aliased / n))
    if aliased > exact:
        print("                     %d were right but worded differently; those"
              % (aliased - exact))
        print("                     are alias-table entries, not model errors")

    print("\nper expected topic")
    for topic, v in sorted(per_topic.items(), key=lambda kv: -kv[1]["n"]):
        bar = "#" * int(round(20.0 * v["ok"] / v["n"]))
        print("   %-18s %2d/%-2d %-20s %5.1f%%"
              % (topic, v["ok"], v["n"], bar, 100.0 * v["ok"] / v["n"]))

    if wrong:
        print("\nmisfiled (%d)" % len(wrong))
        print("   %-16s %-16s %s" % ("EXPECTED", "ASSIGNED", "HEADLINE"))
        for want, got, head in wrong[:25]:
            print("   %-16s %-16s %s" % (want, got or "(none)", head))
    return 0


def stability(c, n: int) -> int:
    """
    Label the same articles twice and report agreement.

    Temperature is zero, so a perfectly deterministic model would score 100%.
    It will not: batching means an article's prompt neighbours differ between
    runs, and a small model is influenced by them. That influence is exactly
    what this measures, and it puts a ceiling on accuracy -- a model that
    agrees with itself 70% of the time cannot be 90% right.
    """
    rows = list(c.articles.aggregate([
        {"$match": {"body": {"$nin": ["", None]}}},
        {"$sample": {"size": int(n)}}]))
    if not rows:
        print("no articles with text to test")
        return 2

    print("labelling %d articles twice ..." % len(rows), flush=True)
    a = classify.classify(rows)
    b = classify.classify(rows)

    same = sum(1 for x, y in zip(a, b) if x["category"] == y["category"])
    print("\nagreement       %d/%d  %5.1f%%"
          % (same, len(rows), 100.0 * same / len(rows)))
    diffs = [(x["category"], y["category"], r.get("headline", "")[:48])
             for r, x, y in zip(rows, a, b) if x["category"] != y["category"]]
    if diffs:
        print("\ndisagreed (%d)" % len(diffs))
        print("   %-16s %-16s %s" % ("RUN 1", "RUN 2", "HEADLINE"))
        for x, y, head in diffs[:25]:
            print("   %-16s %-16s %s" % (x, y, head))
    return 0


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sample", type=int, metavar="N",
                   help="draw N articles to grade by hand, as JSON on stdout")
    g.add_argument("--score", metavar="FILE",
                   help="grade a filled-in sample file")
    g.add_argument("--stability", type=int, metavar="N",
                   help="label N articles twice and report agreement")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--db", help="database name")
    args = ap.parse_args()

    config.load_env()
    if args.score:
        return score(args.score)

    c = Corpus(db=args.db, quiet=True)
    if args.sample:
        rows = draw(c, args.sample, args.seed)
        if not rows:
            print("no labelled articles to sample. Run a reclassify first.",
                  file=sys.stderr)
            return 2
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        print("\n%d rows written. Fill in \"expected\" for each, then:"
              % len(rows), file=sys.stderr)
        print("   python scripts/classify_check.py --score <file>",
              file=sys.stderr)
        return 0
    return stability(c, args.stability)


if __name__ == "__main__":
    raise SystemExit(main())
