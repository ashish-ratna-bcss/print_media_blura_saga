"""
Tell a story apart from the page furniture printed around it.

DocLayout-YOLO returns every text block on the page, and a newspaper page is
mostly not stories: it is datelines, bylines, agency credits, box labels and
whatever the OCR made of a photo caption. Those blocks arrive here looking
exactly like articles -- they have a bbox and some text -- and downstream
everything treats them as stories. They get transcribed, classified, indexed
and counted, so a front page of nine stories reports twenty-three articles and
the topic counts are diluted by things nobody would call an article.

`corpus.ingest` already refused blocks with no headline and almost no body.
That misses the whole problem: a dateline BECOMES the headline, so the block
has a headline and sails through.

Nothing here deletes. A match sets the block's type to FRAGMENT, which keeps
it in the job's output and on the annotated page while removing it from the
article count, the classifier queue and the corpus. Deciding wrongly is
therefore recoverable by eye, which matters because the OCR these patterns run
against is itself unreliable -- "16 अगस्त" arrives as "16 अणस्त" often enough
that anything stricter than a shape match would miss half of them.

Every rule requires a near-empty body first. A block with real body text is a
story no matter what its first line looks like.
"""

import re
import unicodedata

FRAGMENT = "fragment"

# Below this the block has no story in it and the headline is all there is to
# judge. 40 matches the threshold ingest already used for the same question.
THIN_BODY = 40

# Letters left after stripping punctuation, digits and spaces. "ae thei" has
# six, "iene" four; the shortest real headline seen in the archive
# ("ऐसे करता है काम") has twelve.
MIN_LETTERS = 12

# A day and month ending the line: "मुंबई, 16 अणस्त (एजेंसी)",
# "New Delhi, 16 August (PTI)".
#
# The date alone is not enough to go on. Ending at a number and one word also
# describes "Gandhi's 688 handwritten notes ... fetch record 16.2 cr", a real
# headline this rule marked as furniture on the first pass. What separates the
# two is the punctuation a dateline is always printed with: the comma after
# the place, the "।" the reporter's name is separated by, or the agency in
# brackets. One of those is required, and the false positive has none.
# The place may sit between the separator and the date -- a byline runs
# "reporter । agency place date" -- so a short run of non-digits is allowed
# between them. Widening it that far was checked against the archive and adds
# only the two bylines it was meant to catch.
_DATELINE = re.compile(
    r"(?:[,;।|][^\d]{0,25}\d{1,2}\s*[.,]?\s*[^\s\d]{2,14}"   # "जेठी।निस नरवाना 6 अगस्त"
    r"|\d{1,2}\s*[.,]?\s*[^\s\d]{2,14}\s*\([^)]{2,18}\))"     # "16 Aug (PTI)"
    r"\s*(?:\([^)]{2,18}\))?\s*$")

# The byline separator Devanagari papers print between reporter and place.
_BYLINE_SEP = re.compile(r"[।|]")

# Nothing but punctuation, or an ellipsis where a sentence should start: the
# tail of a story whose beginning is in another block.
_STARTS_MID = re.compile(r"^[\s.…·•\-–—'\"]*(?:\.{2,}|…)")


def _letters(text: str) -> int:
    """
    Letter count that Devanagari survives.

    str.isalpha() is False for the vowel signs and anusvara Hindi is written
    with -- they are combining marks, category Mn/Mc -- so it scores "मुंबई"
    as three letters rather than five and undercounts every Hindi headline by
    roughly a third. Counting the marks alongside the letters they attach to
    is what keeps the noise threshold meaning the same thing in both scripts.
    """
    return sum(1 for c in text
               if c.isalpha() or unicodedata.category(c) in ("Mn", "Mc"))


def reason(article: dict) -> str:
    """
    Why this block is not a story, or "" if it is one.

    The string is kept and shown rather than reduced to a boolean, so a page
    that loses a block to this can be asked which rule took it.
    """
    if (article.get("type") or "article") != "article":
        return ""                      # ads and mastheads are already handled

    head = (article.get("headline") or "").strip()
    body = (article.get("body") or "").strip()

    if len(body) >= THIN_BODY:
        return ""                      # it has a story in it

    if not head:
        return "empty" if not body else ""

    if _STARTS_MID.match(head):
        return "continuation"

    # Measured over the whole block, not the headline alone. A short headline
    # with a couple of lines under it is a brief, not noise -- judging the
    # headline by itself rejected "Headline" + 36 characters of body, which is
    # thin but is plainly a story. Genuine noise ("ae thei", "iene") has an
    # empty body, so nothing real is let through by counting both.
    if _letters("%s %s" % (head, body)) < MIN_LETTERS:
        return "noise"

    if _DATELINE.search(head):
        # A byline runs into the dateline on one line often enough that the
        # two are not worth separating; the block is furniture either way.
        return "byline" if _BYLINE_SEP.search(head) else "dateline"

    return ""


def split(articles: list) -> tuple:
    """
    Partition into stories and furniture, marking the furniture in place.

    Returns (stories, fragments). The fragments keep their geometry and text
    and stay in the caller's list -- they are marked, not dropped -- so the
    Result view still draws them on the page.

    Only blocks still typed "article" are candidates. Ads and mastheads belong
    to neither list: they are page furniture too, but something else already
    decided that and they must not be handed back as stories. Blocks marked
    FRAGMENT by an earlier pass return to the fragment list rather than the
    story list, which is what makes running this twice a no-op instead of
    quietly feeding furniture to the classifier.
    """
    stories, found = [], []
    for a in articles:
        kind = a.get("type") or "article"
        if kind == FRAGMENT:
            found.append(a)
            continue
        if kind != "article":
            continue                   # ad, masthead: not ours to judge
        why = reason(a)
        if why:
            a["type"] = FRAGMENT
            a["fragment_reason"] = why
            found.append(a)
        else:
            stories.append(a)
    return stories, found
