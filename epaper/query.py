"""
Query refinements: exclusions, proximity, timeframes, weekdays.

Pure functions over text and dates. No database, no model, no network -- which
is what lets the same rules run in two very different places without drifting
apart: MongoDB narrows the archive with them, and the page-fetching topic job
applies them to articles it has just read and never stored.

Each one exists because the search had no way to express something people kept
wanting:

  exclude     "Delhi Police, but nothing about traffic" -- a single recurring
              subject can otherwise swamp a day's results.
  proximity   "Delhi Police" as a unit, not "Delhi" in the dateline and
              "police" in paragraph nine.
  timeframe   a week of editions rather than one date.
  weekdays    Sunday papers carry the opinion pages; comparing like with like
              means being able to ask for one weekday across a month.
"""

import datetime as _dt
import re

# Word characters across the scripts in the archive. \w in Python is already
# Unicode-aware, so Devanagari is covered; the explicit class is here to keep
# the intent visible rather than resting on a default that could be flagged off.
_WORD = re.compile(r"[\wऀ-ॿ]+", re.UNICODE)

# The proximity settings the UI offers, as word windows. `None` means the
# constraint does not apply.
WINDOWS = {"exact": 0, "w5": 5, "w10": 10, "any": None}
DEFAULT_PROXIMITY = "any"

TIMEFRAMES = ("today", "yesterday", "3d", "week", "month",
              "custom", "all")
DEFAULT_TIMEFRAME = "today"


def tokens(text: str) -> list:
    return _WORD.findall((text or "").lower())


# -- exclusions -------------------------------------------------------------

def parse_terms(text) -> list:
    """
    "traffic, parking , challan" -> ["traffic", "parking", "challan"].

    Accepts a list as well as a string, because the same value arrives from a
    JSON payload one way and from a stored spec the other.
    """
    if not text:
        return []
    if isinstance(text, (list, tuple)):
        raw = list(text)
    else:
        raw = re.split(r"[,\n;]+", str(text))
    out = []
    for t in raw:
        t = " ".join(str(t).lower().split())
        if t and t not in out:
            out.append(t)
    return out


def excluded_by(text: str, terms: list) -> str:
    """
    The first excluded term this text contains, or "".

    Whole-word matching, not substring. "car" excluding an article about
    "carnival" is the kind of silent over-filtering nobody reports as a bug --
    they just conclude the search is bad. A multi-word term is matched as a
    consecutive run of words for the same reason.
    """
    if not terms:
        return ""
    words = tokens(text)
    if not words:
        return ""
    joined = " %s " % " ".join(words)
    for t in terms:
        parts = tokens(t)
        if not parts:
            continue
        if (" %s " % " ".join(parts)) in joined:
            return t
    return ""


def mongo_exclusion(terms: list, fields=("full_text", "headline", "body")) -> dict:
    """
    The filter clause that keeps excluded articles out of a corpus query.

    Regex rather than $text: MongoDB's text index treats a negated term as
    "score this lower", not "never return this", so a $text-based exclusion
    leaks the exact articles it was asked to suppress. Anchored on word
    boundaries to match excluded_by, so the archive path and the page path
    cannot disagree about what "traffic" excludes.
    """
    if not terms:
        return {}
    clauses = []
    for t in terms:
        parts = [re.escape(p) for p in tokens(t)]
        if not parts:
            continue
        pattern = r"\b%s\b" % r"\s+".join(parts)
        clauses.append({"$nor": [{f: {"$regex": pattern, "$options": "i"}}
                                 for f in fields]})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


# -- proximity --------------------------------------------------------------

def within(text: str, phrase: str, window) -> bool:
    """
    Whether the phrase's words occur close enough together in the text.

    `window` is the number of words allowed between the first and last of
    them; 0 demands the exact phrase, and None removes the constraint. A
    single-word phrase satisfies every window, so proximity quietly degrades
    to "does the word appear" rather than rejecting everything.

    Scans word positions rather than using a regex, because the window is a
    count of words and a regex would have to encode it as a character span --
    which is not the same thing in Hindi, where words are shorter.
    """
    if window is None:
        return True
    words = tokens(phrase)
    if not words:
        return True
    hay = tokens(text)
    if not hay:
        return False

    if len(words) == 1:
        return words[0] in hay

    if window == 0:                       # exact phrase, in order
        n = len(words)
        return any(hay[i:i + n] == words
                   for i in range(0, max(0, len(hay) - n + 1)))

    # Smallest span containing every word, in any order. Walk the text once,
    # remembering where each word was last seen; a span is complete when all
    # of them have been seen, and its width is the current position minus the
    # earliest of those sightings.
    want = set(words)
    last = {}
    for i, w in enumerate(hay):
        if w not in want:
            continue
        last[w] = i
        if len(last) == len(want):
            span = i - min(last.values())
            if span - (len(want) - 1) <= window:
                return True
    return False


def window_for(proximity) -> int | None:
    """The word window for a UI setting, defaulting to no constraint."""
    return WINDOWS.get(str(proximity or DEFAULT_PROXIMITY).lower(),
                       WINDOWS[DEFAULT_PROXIMITY])


# -- timeframes -------------------------------------------------------------

def resolve_timeframe(preset, *, today=None, date_from=None, date_to=None):
    """
    A preset and a today into an inclusive (from, to) pair of ISO dates.

    Returns (None, None) for "all", which every caller reads as "no date
    filter" -- distinct from a range that happens to be empty.

    `today` is a parameter rather than a call to date.today() so the behaviour
    is testable and so a nightly run started at 23:59 and finishing at 00:02
    cannot silently change which days it was asked about.
    """
    p = str(preset or DEFAULT_TIMEFRAME).lower()
    end = today or _dt.date.today()
    if isinstance(end, str):
        end = _dt.date.fromisoformat(end)

    if p == "all":
        return None, None
    if p == "custom":
        # An inverted range is a slip, not an empty result: somebody picked
        # the dates in the order they think of them. Swapping is what they
        # meant, and returning nothing would look like missing data.
        a, b = date_from or None, date_to or None
        if a and b and a > b:
            a, b = b, a
        return a, b

    # Yesterday is the only preset that is not a window ending today: it is
    # one specific day. A reader asking for yesterday's paper wants that
    # paper, not today's-and-yesterday's.
    if p == "yesterday":
        y = (end - _dt.timedelta(days=1)).isoformat()
        return y, y

    days = {"today": 0, "3d": 2, "week": 6, "month": 29}.get(p)
    if days is None:
        return end.isoformat(), end.isoformat()
    return (end - _dt.timedelta(days=days)).isoformat(), end.isoformat()


# -- weekdays ---------------------------------------------------------------

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_weekdays(value) -> list:
    """
    Day selections into Python weekday numbers (Monday is 0).

    Accepts names or numbers, since the UI sends names and a stored spec may
    round-trip either. An empty or full selection returns [] -- both mean
    "every day", and collapsing them here saves every caller a special case.
    """
    if not value:
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out = set()
    for v in items:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            if 0 <= v <= 6:
                out.add(v)
            continue
        s = str(v).strip().lower()[:3]
        if s in DAY_NAMES:
            out.add(DAY_NAMES.index(s))
    if len(out) == 7:
        return []
    return sorted(out)


def on_weekday(date_str: str, weekdays: list) -> bool:
    """Whether an ISO date falls on one of the selected days."""
    if not weekdays:
        return True
    try:
        return _dt.date.fromisoformat(str(date_str)[:10]).weekday() in weekdays
    except (ValueError, TypeError):
        # An undated article cannot be shown to fall on a Tuesday, and
        # guessing would put it in a comparison it does not belong to.
        return False


def mongo_weekdays(weekdays: list) -> dict:
    """
    The filter clause selecting those weekdays.

    `date` is stored as an ISO string, so this converts it server-side rather
    than reading every candidate into Python. $dayOfWeek numbers Sunday as 1;
    Python numbers Monday as 0, hence the remap.
    """
    if not weekdays:
        return {}
    mongo_days = [((d + 1) % 7) + 1 for d in weekdays]
    return {"$expr": {"$in": [
        {"$dayOfWeek": {"$dateFromString": {
            "dateString": {"$substrBytes": ["$date", 0, 10]},
            "onError": None, "onNull": None}}},
        mongo_days]}}
