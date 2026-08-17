"""
Keyword expansion: turn one word into the vocabulary a beat is actually written in.

A reader who asks for "Delhi Police" does not want the stories containing the
string "police". They want the beat -- the arrest, the FIR, the chargesheet, the
DCP's statement, the thana that was gheraoed -- most of which never use the word.
Matching the query literally finds a fraction of the beat and looks broken.

So a query is first resolved to a BEAT: a hand-built, weighted, bilingual
vocabulary. Every term carries a weight, because the terms are not equally
informative. "constable" alone establishes the subject; "arrested" alone does
not (companies arrest slides, courts arrest judgments); "Tis Hazari" only
supports a case another term already made.

    CORE     1.0   the force and its people -- the subject itself
    RELATED  0.6   what police do, and what is done to them
    CONTEXT  0.3   the surrounding furniture: courts, jails, Delhi place names

This module is deliberately dumb and free. It exists next to topics.py, which is
neither, and it earns its place three ways:

  PREFILTER  A body with zero hits against a 300-term bilingual vocabulary is
             not about the beat. Dropping those before topics.screen() saves
             LLM calls that were always going to come back "no".
  FALLBACK   When the model server is down, lexical ranking still answers. A
             degraded answer beats a stack trace.
  EVIDENCE   The matched terms are stored per article, so a result can be
             explained without asking a model why it said what it said.

Where the beats come from
-------------------------
The default set is built at import from epaper/beatdata.py -- one shared
policing pack plus the geography of all 28 states and 8 union territories, so
`BEATS` holds 36 beats with no database, no model and no network. That matters:
scripts/topic_check.py and the prefilter both run in environments that have
none of those.

Beats can then be REPLACED at runtime by `load()`, which is how epaper/beats.py
installs whatever MongoDB holds -- edits, additional terms, new beats added
from the backend. The built-in set is the floor, never the ceiling, and a
database that is down leaves the module fully working rather than empty.

Devanagari note: Hindi inflects by suffix (थाना -> थाने, गिरफ्तार -> गिरफ्तारी),
so Devanagari terms prefix-match by default while Latin terms match whole words.
Forms that change the stem rather than extend it (थाना/थाने) are listed twice --
there is no stemmer here and a wrong stemmer is worse than none.
"""

from __future__ import annotations

import re
import unicodedata

from .beatdata import CORE, RELATED, CONTEXT, build_beats  # noqa: F401

# Below this, an article has too little text for "no keyword hit" to mean
# anything -- an unread body is not evidence of irrelevance. Such articles are
# always kept by the prefilter.
MIN_TEXT = 60

# Newspaper text arrives with words fused together -- "Proudoftaggiven",
# "ComplaintslodgedagainstSonia", "Retestof3UGC-NET". This is not an OCR defect
# to be engineered away: the vision model produces the same fusions, because
# the source rendering loses the spaces. Latin terms match on whole-word
# boundaries, so a headline read as "policearrested" scores ZERO and the story
# disappears from its beat -- the worst failure available, because an absent
# result looks identical to no result.
#
# So terms are also tested as substrings, but ONLY inside runs long enough to
# be fused, and only for terms long enough that a chance substring is unlikely.
# Both limits matter: matching substrings across the whole text would let "cop"
# hit inside "copy", and allowing short terms would do the same within a run.
FUSED_RUN = 14        # a word this long is presumed to have lost its spaces
FUSED_TERM = 5        # shorter terms are too collision-prone to match loose

# A CORE term in the headline is worth more than the same term buried in
# paragraph nine: newspapers put the subject in the headline.
HEADLINE_BONUS = 0.25

# Weight needed to reach a score of 1.0. Set so that one CORE term on its own
# does NOT clear a 0.5 threshold: "the minister said police were informed" is a
# passing mention, and a beat vocabulary this wide should find more than one
# term in a story that is genuinely on the beat.
FULL_SCORE = 3.0

# Multiplier for a story that names a DIFFERENT region and never names this
# beat's own. Observed failure: "Khanna police turn to facial recognition"
# scored 1.00 for Delhi Police -- perfect policing vocabulary, wrong state.
#
# Note what this is NOT: a requirement that the story name Delhi. In a Delhi
# edition most local stories never do, because the whole paper is Delhi, so a
# hard geography gate would drop exactly the stories the beat exists to find.
# Only a competing placename is evidence of elsewhere. Silence is not.
OUT_OF_REGION = 0.35

# ...and a ceiling, because the multiplier alone is not enough. A dense enough
# out-of-region story still cleared 0.5 on multiplication ("Mumbai police bust
# drug cartel" scored 0.57 off seven hits). The multiplier keeps the ordering
# among off-patch stories meaningful; the cap guarantees none of them can pass
# a 0.5 threshold on lexical evidence alone. Hybrid mode is unaffected -- the
# prefilter still hands them to the model, which may legitimately disagree.
OUT_OF_REGION_CAP = 0.45

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_PUNCT = re.compile(r"[^\w\sऀ-ॿ]+", re.UNICODE)


# ------------------------------------------------------------------ beats

BEATS: dict = {b["id"]: b for b in build_beats()}

# What build_beats() produced, kept aside so reset() can get back to it after
# a database load has replaced things.
_BUILTIN: dict = dict(BEATS)


def load(beats, *, replace: bool = True):
    """
    Install a set of beats, typically the ones stored in MongoDB.

    `replace=True` swaps the whole table; `replace=False` merges, letting a
    database hold only the beats it has actually customised while the other
    35 keep their built-in definitions. Merge is the safer default for a
    partially-populated database and is what beats.py uses.

    Every cache is dropped, because a stale compiled pattern outliving the
    term list it came from is the kind of bug that shows up as a search
    quietly returning the wrong thing.
    """
    incoming = {b["id"]: b for b in beats if b.get("id")}
    if replace:
        BEATS.clear()
        BEATS.update(incoming)
    else:
        BEATS.update(incoming)
    invalidate()
    return len(BEATS)


def register(beat: dict):
    """Add or overwrite exactly one beat."""
    if not beat.get("id"):
        raise ValueError("a beat needs an id")
    BEATS[beat["id"]] = beat
    invalidate(beat["id"])
    return beat["id"]


def reset():
    """Back to the built-in state vocabulary. Used by tests and by --no-db."""
    BEATS.clear()
    BEATS.update(_BUILTIN)
    invalidate()


def invalidate(beat_id: str | None = None):
    """Drop compiled caches for one beat, or all of them."""
    for cache in (_COMPILED, _REGIONS, _EXTRA, _PACKS):
        if beat_id is None:
            cache.clear()
        else:
            cache.pop(beat_id, None)
    if beat_id is not None:
        _PACKS.clear()          # a pack is shared; one member changing dirties it


def enabled_ids() -> list:
    """Beats a query is allowed to resolve to, in declared order."""
    return [i for i, b in BEATS.items() if b.get("enabled", True)]


# Kept as a module attribute because callers imported it before the state data
# was factored out. It is now just Delhi's entry in the geography table.
DELHI_PLACES = list(BEATS.get("delhi-police", {}).get("region") or [])



# ------------------------------------------------------------ compilation

def _norm(s: str) -> str:
    """NFC, lowercased, punctuation to spaces. For query/alias comparison only."""
    s = unicodedata.normalize("NFC", s or "").lower()
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", s)).strip()


def _pattern(term: str, fold: bool):
    """
    One term to one compiled regex.

    Term syntax: a trailing '*' forces prefix matching, a trailing '=' forces
    whole-word. With neither, Devanagari prefix-matches and Latin does not --
    see the module docstring.

    The left edge is always a non-word boundary, so a term never matches from
    the middle of a longer word. Internal spaces become \\s+ so a phrase still
    matches across the line break a newspaper column puts in the middle of it.
    """
    force_prefix, force_exact = term.endswith("*"), term.endswith("=")
    t = term[:-1] if (force_prefix or force_exact) else term
    prefix = force_prefix or (not force_exact and bool(_DEVANAGARI.search(t)))
    body = r"\s+".join(re.escape(w) for w in unicodedata.normalize("NFC", t).split())
    rx = r"(?<!\w)" + body + ("" if prefix else r"(?!\w)")
    return re.compile(rx, re.UNICODE | (re.IGNORECASE if fold else 0)), t


def _acronym_pattern(a: str):
    """FIR also written F.I.R. Case-sensitive: 'fir' is a tree, 'FIR' is not."""
    body = r"\.?".join(re.escape(c) for c in a) + r"\.?"
    return re.compile(r"(?<!\w)" + body + r"(?!\w)", re.UNICODE), a


def _compile_beat(beat: dict) -> list:
    """[(weight, regex, display_term)], heaviest weight kept on duplicates."""
    best = {}
    for groups, maker in ((beat.get("terms") or [], lambda t: _pattern(t, True)),
                          (beat.get("acronyms") or [], _acronym_pattern)):
        for weight, terms in groups:
            for term in terms:
                rx, shown = maker(term)
                if best.get(shown, (0,))[0] < weight:
                    best[shown] = (weight, rx)
    return [(w, rx, t) for t, (w, rx) in best.items()]


def _alternation(terms, prefix: bool):
    """
    Many place names to ONE regex.

    Geography is a yes/no question -- does this story name anywhere in Punjab?
    -- so the individual match is never needed, only whether any of them hit.
    Compiling one alternation instead of N patterns is what makes 36 beats
    affordable: `region_out` for a single state is now every other state's
    places, around 400 names, and testing those one regex at a time, 36 times
    per article, was 14,000 scans where 72 will do.
    """
    bodies = [r"\s+".join(re.escape(w) for w in unicodedata.normalize("NFC", t).split())
              for t in terms if t]
    if not bodies:
        return None
    rx = r"(?<!\w)(?:" + "|".join(bodies) + ")" + ("" if prefix else r"(?!\w)")
    return re.compile(rx, re.UNICODE | re.IGNORECASE)


def _region_patterns(terms) -> list:
    """
    At most two patterns: Devanagari prefix-matches, Latin does not.

    Splitting on script rather than compiling one mixed alternation keeps the
    same matching rules the per-term path uses -- a Hindi place name has to
    match its inflected forms, an English one must not match inside a longer
    word.
    """
    dev = [t for t in (terms or []) if _DEVANAGARI.search(t)]
    lat = [t for t in (terms or []) if not _DEVANAGARI.search(t)]
    return [p for p in (_alternation(dev, True), _alternation(lat, False)) if p]


_COMPILED: dict = {}
_REGIONS: dict = {}
_EXTRA: dict = {}
_PACKS: dict = {}


def compiled(beat_id: str) -> list:
    if beat_id not in _COMPILED:
        _COMPILED[beat_id] = _compile_beat(BEATS[beat_id])
    return _COMPILED[beat_id]


def regions(beat_id: str) -> tuple:
    """(own-turf patterns, elsewhere patterns) for the geography check."""
    if beat_id not in _REGIONS:
        b = BEATS[beat_id]
        _REGIONS[beat_id] = (_region_patterns(b.get("region")),
                             _region_patterns(b.get("region_out")))
    return _REGIONS[beat_id]


# ---------------------------------------------------------------- resolve

def _fused_terms(patterns: list, beat: dict) -> list:
    """
    (needle, weight, display_term) for substring matching inside fused runs.

    Acronyms are excluded deliberately. They match case-sensitively precisely
    so that "FIR" does not fire on "fir" or "first"; lowercasing them for a
    substring test would hand back exactly the false positives that care was
    taken to avoid.
    """
    acro = {t for _, group in (beat.get("acronyms") or []) for t in group}
    out = []
    for weight, _rx, term in patterns:
        if term in acro:
            continue
        needle = "".join(term.split()).lower().rstrip("*=")
        if len(needle) >= FUSED_TERM:
            out.append((needle, weight, term))
    return out


def _term_count(beat: dict) -> int:
    """Unique terms, counted WITHOUT compiling them."""
    seen = set()
    for group in (beat.get("terms") or []) + (beat.get("acronyms") or []):
        seen.update(group[1])
    return len(seen)


def catalogue(*, enabled_only: bool = True, samples: bool = False) -> list:
    """
    Beats for the UI: what exists, how big each vocabulary is, and where it is.

    Deliberately does not compile anything by default. With one beat, calling
    compiled() per entry to count terms was free; with 36 it meant building
    roughly ten thousand regexes to render a dropdown. Term counts come from
    the raw lists instead, and `samples=True` is there for the one screen that
    actually wants to show them.
    """
    out = []
    for b in BEATS.values():
        if enabled_only and not b.get("enabled", True):
            continue
        row = {
            "id": b["id"],
            "label": b["label"],
            "topic": b.get("topic", ""),
            "kind": b.get("kind", "custom"),
            "state": b.get("state", ""),
            "state_hi": b.get("state_hi", ""),
            "state_code": b.get("state_code", ""),
            "region_kind": b.get("region_kind", ""),
            "builtin": bool(b.get("builtin")),
            "enabled": b.get("enabled", True),
            "n_terms": _term_count(b),
            "n_places": len(b.get("region") or []),
            # Shipped with the list so the UI can tell whether an edition
            # covers this force -- "Jalandhar Tribune" is Punjab's because
            # Jalandhar is one of Punjab's places. Reuses the geography that
            # already exists rather than adding a second table of editions to
            # keep in step with it. 9 KB across all 36, fetched once.
            "places": list(b.get("region") or []),
        }
        if samples:
            row["sample"] = [t for _, _, t in
                             sorted(compiled(b["id"]), key=lambda x: -x[0])[:12]]
        out.append(row)
    return out


def resolve(query: str):
    """
    Best matching beat for a free-text query, or None.

    LONGEST alias wins, and that rule is load-bearing now that there are 36
    beats. Every state beat answers to the bare word "police", so first-match
    ordering sent "Punjab Police" to the Delhi vocabulary -- "police" matched
    inside it and Delhi happened to be first in the table. Preferring the
    longest matching alias makes "punjab police" (13 chars) beat "police" (6),
    while a bare "police" still falls through to whichever beat leads the
    table.

    Still loose on purpose: "delhi police", "traffic police challans" and
    "पुलिस" should all land somewhere sensible. A wrong hit is survivable --
    the beat only widens the search and, in hybrid mode, the model still judges
    relevance against the user's own words, not the beat's.
    """
    q = _norm(query)
    if not q:
        return None
    best, best_len = None, 0
    for beat in BEATS.values():
        if not beat.get("enabled", True):
            continue
        for alias in beat.get("aliases") or []:
            a = _norm(alias)
            if not a:
                continue
            if a == q:
                return beat                       # exact match cannot be beaten
            if len(a) > best_len and re.search(
                    r"(?<!\w)%s(?!\w)" % re.escape(a), q):
                best, best_len = beat, len(a)
    return best


def expand(query: str) -> dict:
    """
    A query to the vocabulary that will actually be searched for.

    On a miss there is no expansion -- the query's own words are used and
    `expanded` is False, which tells prefilter() it has no right to drop
    anything. Inventing synonyms for an unknown topic is how you lose stories.
    """
    beat = resolve(query)
    if beat:
        here, elsewhere = regions(beat["id"])
        pats = compiled(beat["id"])
        return {"query": query, "beat": beat["id"], "label": beat["label"],
                "topic": beat["topic"], "expanded": True,
                "patterns": pats,
                "terms": [t for _, _, t in pats],
                "fused": _fused_terms(pats, beat),
                "region": here, "region_out": elsewhere}
    words = [w for w in _norm(query).split() if len(w) > 2]
    pats = [(CORE, _pattern(w, True)[0], w) for w in words]
    return {"query": query, "beat": None, "label": query.strip(),
            "topic": query.strip(), "expanded": False,
            "patterns": pats, "terms": words,
            "fused": [(w, CORE, w) for w in words if len(w) >= FUSED_TERM],
            "region": [], "region_out": []}


# ------------------------------------------------------------------ match

def _text_of(article: dict, limit: int = 6000) -> tuple:
    head = " ".join(str(article.get(k) or "")
                    for k in ("headline", "byline", "dateline")).strip()
    body = str(article.get("body") or article.get("summary")
               or article.get("content") or "")[:limit]
    return head, unicodedata.normalize("NFC", head + "\n" + body)


def _evidence(head: str, full: str, patterns, fused) -> tuple:
    """
    (hits, weight, headline_hit) for one pattern set against one article.

    Split out of score() so score_all() can run the shared policing patterns
    once and reuse the result across all 36 state beats.

    Distinct terms are summed, not occurrences: a story saying "police" nine
    times is one piece of evidence, while a story saying "police", "FIR" and
    "chargesheet" once each is three.
    """
    hits, total, in_headline = [], 0.0, False
    for weight, rx, term in patterns:
        if not rx.search(full):
            continue
        hits.append(term)
        total += weight
        if weight >= CORE and not in_headline and rx.search(head):
            in_headline = True
    # Second pass over fused runs only. See FUSED_RUN: a story whose spaces
    # were lost in rendering must still reach its beat.
    runs = " ".join(w.lower() for w in full.split() if len(w) >= FUSED_RUN)
    if runs and fused:
        seen = set(hits)
        head_runs = " ".join(w.lower() for w in head.split() if len(w) >= FUSED_RUN)
        for needle, weight, term in fused:
            if term in seen or needle not in runs:
                continue
            hits.append(term)
            seen.add(term)
            total += weight
            if weight >= CORE and not in_headline and needle in head_runs:
                in_headline = True
    return hits, total, in_headline


def _finish(hits, total, in_headline, here, elsewhere, full) -> dict:
    """Headline bonus, then geography, then normalise. Shared by both scorers."""
    if in_headline:
        total += HEADLINE_BONUS
    off_patch = elsewhere and not here
    if off_patch:
        total = min(total * OUT_OF_REGION, FULL_SCORE * OUT_OF_REGION_CAP)
    return {"hits": hits, "weight": round(total, 2),
            "score": round(min(1.0, total / FULL_SCORE), 2),
            "headline_hit": in_headline, "in_region": here,
            "off_patch": off_patch,
            "testable": len(full.strip()) >= MIN_TEXT}


def score(article: dict, spec: dict) -> dict:
    """Lexical evidence that one article belongs to one beat."""
    head, full = _text_of(article)
    head = unicodedata.normalize("NFC", head)
    hits, total, in_headline = _evidence(head, full, spec["patterns"],
                                         spec.get("fused"))
    # Geography last, applied to the finished total. A story can have perfect
    # policing vocabulary and still be another force's beat.
    here = any(rx.search(full) for rx in spec.get("region") or [])
    elsewhere = any(rx.search(full) for rx in spec.get("region_out") or [])
    return _finish(hits, total, in_headline, here, elsewhere, full)


# ------------------------------------------------------- scoring every beat

def _split_terms(beat: dict) -> tuple:
    """
    (shared term groups, this beat's own place names).

    A state beat is the shared policing pack plus its geography, merged into
    one list at build time. Pulling the geography back out is what lets the
    pack be compiled and matched once for all 36 beats instead of 36 times.
    Only beats that declare a `pack` are split; a hand-written beat is treated
    as entirely its own and takes the ordinary path.
    """
    if not beat.get("pack"):
        return (beat.get("terms") or []), []
    places = set(beat.get("region") or [])
    shared = []
    for weight, terms in (beat.get("terms") or []):
        kept = [t for t in terms if t not in places]
        if kept:
            shared.append((weight, kept))
    return shared, sorted(places)


def _pack_of(beat: dict) -> tuple:
    """
    ((patterns, fused), places) with the pattern half cached across beats.

    Keyed on a fingerprint of the shared terms themselves rather than on the
    pack name, so two beats share a compile exactly when their vocabularies
    are actually identical. A beat edited from the backend diverges, gets its
    own compile, and stays correct -- which a name-keyed cache would not.
    """
    shared, places = _split_terms(beat)
    acro = beat.get("acronyms") or []
    key = (tuple((w, tuple(t)) for w, t in shared),
           tuple((w, tuple(t)) for w, t in acro))
    hit = _PACKS.get(key)
    if hit is None:
        pats = _compile_beat({"terms": shared, "acronyms": acro})
        hit = _PACKS[key] = (pats, _fused_terms(pats, {"acronyms": acro}))
    return hit, places


def _places_of(beat_id: str, places: list) -> tuple:
    """This beat's own geography, compiled as ordinary CONTEXT terms."""
    if beat_id not in _EXTRA:
        pats = _compile_beat({"terms": [(CONTEXT, places)]}) if places else []
        _EXTRA[beat_id] = (pats, _fused_terms(pats, {}))
    return _EXTRA[beat_id]


def score_all(article: dict, beat_ids=None) -> dict:
    """
    Score one article against every enabled beat at once. {beat_id: score}.

    The reason this exists rather than a loop over score(): every state beat
    shares one ~290-term policing vocabulary and differs only in geography.
    Scoring them independently re-ran the identical 290 patterns 36 times per
    article. Here the pack runs once and each beat adds only its own place
    names and its region gate.

    Two further savings fall out of the same structure. A packed beat's
    `region` list IS its place-term list, so whether the story is on this
    beat's turf is already known from the place hits -- the `region`
    alternation never has to run. And `headline_hit` can only be set by a CORE
    term, which places never are, so it comes from the pack unchanged.

    Results are identical to calling score() per beat; test_beats.py asserts
    that on every beat rather than trusting the reasoning.
    """
    head, full = _text_of(article)
    head = unicodedata.normalize("NFC", head)
    ids = list(BEATS) if beat_ids is None else list(beat_ids)
    out, seen_packs = {}, {}

    for bid in ids:
        beat = BEATS.get(bid)
        if not beat or not beat.get("enabled", True):
            continue
        (pats, fused), places = _pack_of(beat)
        key = id(pats)
        if key not in seen_packs:
            seen_packs[key] = _evidence(head, full, pats, fused)
        base_hits, base_total, in_headline = seen_packs[key]

        hits, total = list(base_hits), base_total
        if places:
            p_pats, p_fused = _places_of(bid, places)
            p_hits, p_total, _ = _evidence(head, full, p_pats, p_fused)
            hits += p_hits
            total += p_total
            here = bool(p_hits)          # region == places for a packed beat
            elsewhere = any(rx.search(full) for rx in regions(bid)[1])
        else:
            here_p, out_p = regions(bid)
            here = any(rx.search(full) for rx in here_p)
            elsewhere = any(rx.search(full) for rx in out_p)

        out[bid] = _finish(hits, total, in_headline, here, elsewhere, full)
    return out


def prefilter(articles: list, spec: dict) -> dict:
    """
    Indices still worth an LLM call, plus the scores, so nothing is recomputed.

    Drops an article only when all three hold: the vocabulary was expanded from
    a known beat, the article has enough text for absence to mean something,
    and it matched nothing at all. Fails open -- if that would drop every
    article the vocabulary is more likely wrong than the edition, so keep all.
    """
    scores = [score(a, spec) for a in articles]
    if not spec.get("expanded"):
        return {"keep": list(range(len(articles))), "scores": scores,
                "dropped": 0, "reason": "no expansion; nothing dropped"}
    keep = [i for i, s in enumerate(scores) if s["hits"] or not s["testable"]]
    if not keep:
        return {"keep": list(range(len(articles))), "scores": scores,
                "dropped": 0, "reason": "no article matched; kept all"}
    return {"keep": keep, "scores": scores,
            "dropped": len(articles) - len(keep),
            "reason": "%d of %d had no term from the %s vocabulary"
                      % (len(articles) - len(keep), len(articles),
                         spec.get("label") or "query")}


def adjust(model_score: float, article: dict, spec: dict) -> dict:
    """
    Apply the beat's geography to a score the MODEL produced.

    This has to exist separately from score(). In hybrid mode the final number
    comes from topics.assess(), so a gate that lives only inside score() is
    consulted for the prefilter and then silently ignored for the result the
    user actually sees. Observed exactly that: after the region gate was added,
    "Khanna police turn to facial recognition" still came back at 0.90, because
    nothing on the hybrid path ever asked.

    Geography is a property of the beat, not of the engine that ranked it, so
    both engines run it. Returns the adjusted score and why, for display.
    """
    s = score(article, spec)
    out = {"score": round(float(model_score), 2), "hits": s["hits"],
           "off_patch": s["off_patch"], "reason": ""}
    if s["off_patch"]:
        out["score"] = round(min(float(model_score) * OUT_OF_REGION,
                                 OUT_OF_REGION_CAP), 2)
        out["reason"] = "names another region and never %s" % (
            spec.get("label") or "this beat")
    return out


def rank(articles: list, spec: dict, min_score: float = 0.5) -> list:
    """
    Relevance without a model. Used when the LLM is unavailable or declined.

    Recall-oriented and openly so: it cannot tell a story about the police from
    a story that quotes one. topics.assess exists for that.
    """
    out = []
    for a in articles:
        s = score(a, spec)
        if s["score"] < min_score:
            continue
        why = "matched %s" % ", ".join(s["hits"][:6])
        if s["off_patch"]:
            why += " (names another region; scored down)"
        out.append({**a, "topic_score": s["score"], "keywords": s["hits"],
                    "topic_why": why, "topic_summary": ""})
    out.sort(key=lambda m: -m["topic_score"])
    return out
