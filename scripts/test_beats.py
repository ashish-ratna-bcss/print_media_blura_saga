#!/usr/bin/env python3
"""
Regression tests for the beat vocabulary. No model, no GPU, no network.

    python scripts/test_beats.py            # everything that needs no database
    python scripts/test_beats.py --db       # also the MongoDB round-trip

Four things are checked, each of which has actually broken:

  EQUIVALENCE  score_all() shares one compile of the policing pack across all
               36 beats. That is an optimisation, and an optimisation that
               changes results is a bug. Every beat is scored both ways and
               the numbers must match exactly.

  ROUTING      Every beat answers to the bare word "police", so first-match
               resolution sent "Punjab Police" to Delhi. Longest-alias wins
               now; these cases pin it.

  GEOGRAPHY    A place name that is also an ordinary word produces false
               geography. "गया" is Gaya in Bihar and also the commonest Hindi
               past tense, so Bihar scored 1.00 on a Mumbai story. The neutral
               sentences below must match no region at all.

  ROUND-TRIP   Beats loaded from MongoDB must score identically to the
               built-in ones, or an edit made from the backend silently means
               something different from the same edit made in code.

Writes nothing. --db reads the beats collection but never modifies it.
"""

from __future__ import annotations

import argparse
import io
import os
import sys

# Half the fixtures are Devanagari and the Windows console is cp1252, which
# cannot encode it -- without this the suite dies on its own output rather
# than on a failing assertion.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import beatdata, keywords  # noqa: E402

FIXTURES = [
    ("punjab", "punjab-police",
     "Ludhiana police arrest three in snatching case",
     "The Punjab Police said an FIR was registered at the Khanna thana after "
     "constables recovered a pistol. A chargesheet will follow."),
    ("delhi", "delhi-police",
     "Delhi Police bust gang in Rohini",
     "A DCP said the crime branch raided a flat in Dwarka and arrested four. "
     "The accused were produced at Tis Hazari."),
    ("maharashtra", "maharashtra-police",
     "मुंबई पुलिस ने तस्करी का भंडाफोड़ किया",
     "ठाणे में छापेमारी कर अवैध हथियार बरामद किए गए। आरोपी को गिरफ्तार किया गया।"),
    ("bihar", "bihar-police",
     "पटना पुलिस ने चोरी के आरोपी को दबोचा",
     "बिहार पुलिस ने भागलपुर में छापेमारी की। थाना प्रभारी ने बताया कि मुकदमा दर्ज हुआ।"),
    ("gujarat", "gujarat-police",
     "Surat police seize heroin worth crores",
     "Gujarat Police said the consignment was recovered near Vadodara."),
    ("kerala", "kerala-police",
     "Kochi police register case over cyber fraud",
     "Kerala Police said the complaint came from a Thrissur resident."),
    ("none", None,
     "Sensex climbs 400 points on banking rally",
     "Markets closed higher as investors bought financial stocks."),
]

# Sentences with policing vocabulary and NO place. Nothing here may trip a
# region gate: a false geography hit is what the exclusion list in
# beatdata.py exists to prevent, and it regresses silently.
NEUTRAL = [
    "पुलिस ने आरोपी को गिरफ्तार किया गया और मुकदमा दर्ज किया गया।",
    "थाना प्रभारी ने बताया कि जांच की जा रही है और चार्जशीट दाखिल किया जाएगा।",
    "यह काम पूरा किया गया, फिर आरोपी को अदालत भेज दिया गया।",
    "The constable said an FIR was registered and the accused was remanded.",
    "Police recovered a pistol and filed a chargesheet in the court.",
]

ROUTING = [
    ("Delhi Police", "delhi-police"),
    ("Punjab Police", "punjab-police"),
    ("punjab police", "punjab-police"),
    ("Kerala Police", "kerala-police"),
    ("Uttar Pradesh Police", "uttar-pradesh-police"),
    ("Tamil Nadu Police", "tamil-nadu-police"),
    ("पंजाब पुलिस", "punjab-police"),
    ("दिल्ली पुलिस", "delhi-police"),
    ("Jammu and Kashmir Police", "jammu-and-kashmir-police"),
    # A bare topic word has no state in it, so it lands on the table's first
    # entry. That is Delhi by construction, not by accident.
    ("police", "delhi-police"),
    ("sensex and the bond market", None),
]

FAILED = []


def check(name: str, ok: bool, detail: str = ""):
    print("  %-5s %s%s" % ("ok" if ok else "FAIL", name,
                           "" if ok else "   %s" % detail))
    if not ok:
        FAILED.append(name)


def test_equivalence():
    print("\nequivalence -- score_all() against score() per beat")
    worst = 0
    for name, _, head, body in FIXTURES:
        art = {"headline": head, "body": body}
        fast = keywords.score_all(art)
        bad = []
        for bid in keywords.BEATS:
            slow = keywords.score(art, keywords.expand(keywords.BEATS[bid]["label"]))
            if bid not in fast:
                bad.append("%s missing" % bid)
                continue
            f = fast[bid]
            if (round(f["score"], 4) != round(slow["score"], 4)
                    or f["off_patch"] != slow["off_patch"]
                    or sorted(f["hits"]) != sorted(slow["hits"])):
                bad.append("%s %.3f/%s vs %.3f/%s" % (bid, f["score"],
                                                      f["off_patch"],
                                                      slow["score"],
                                                      slow["off_patch"]))
        worst = max(worst, len(bad))
        check("%-14s (%d beats)" % (name, len(fast)), not bad,
              "; ".join(bad[:3]))
    return worst == 0


def test_routing():
    print("\nrouting -- longest alias wins")
    for query, want in ROUTING:
        got = keywords.resolve(query)
        got_id = got["id"] if got else None
        check("%-26s -> %s" % (repr(query), got_id), got_id == want,
              "expected %s" % want)


def test_geography():
    print("\ngeography -- placeless copy must not match any region")
    for text in NEUTRAL:
        art = {"headline": "", "body": text}
        tripped = []
        for bid, s in keywords.score_all(art).items():
            if s["in_region"]:
                tripped.append(bid)
        label = text[:44] + ("..." if len(text) > 44 else "")
        check(label, not tripped,
              "matched region of %s" % ", ".join(tripped[:4]))


def test_ownership():
    print("\nownership -- the right force claims the story")
    for name, want, head, body in FIXTURES:
        art = {"headline": head, "body": body}
        on = [(b, s["score"]) for b, s in keywords.score_all(art).items()
              if s["hits"] and not s["off_patch"]]
        on.sort(key=lambda x: -x[1])
        got = on[0][0] if on else None
        check("%-14s -> %s" % (name, got), got == want,
              "expected %s, got %s" % (want, [b for b, _ in on[:3]]))


def test_roundtrip():
    print("\nround-trip -- MongoDB beats must equal built-in beats")
    from epaper import beats as beats_mod
    store = beats_mod.store()
    if not store:
        check("mongodb", False, beats_mod.last_error())
        return
    keywords.reset()
    before = {n: keywords.score_all({"headline": h, "body": b})
              for n, _, h, b in FIXTURES}
    store.load_into_keywords()
    after = {n: keywords.score_all({"headline": h, "body": b})
             for n, _, h, b in FIXTURES}
    for name in before:
        diff = [bid for bid in before[name]
                if bid not in after[name]
                or round(before[name][bid]["score"], 4)
                != round(after[name][bid]["score"], 4)]
        check("%-14s (%d beats)" % (name, len(after[name])), not diff,
              "differ on %s" % ", ".join(diff[:3]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="store_true",
                    help="also test the MongoDB round-trip")
    a = ap.parse_args()

    print("%d beats built in (%d states, %d union territories), pack of %d terms"
          % (len(keywords.BEATS),
             sum(1 for s in beatdata.STATES if s.get("kind") == "state"),
             sum(1 for s in beatdata.STATES if s.get("kind") == "ut"),
             sum(len(t) for _, t in beatdata.POLICING_TERMS)))

    test_equivalence()
    test_routing()
    test_geography()
    test_ownership()
    if a.db:
        test_roundtrip()

    print("\n%s" % ("all passed" if not FAILED
                    else "%d FAILED: %s" % (len(FAILED), "; ".join(FAILED[:6]))))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
