"""
Telling the five police headings apart, where the model cannot.

The archive files police stories under crime / investigation / policing /
security / traffic. That split is the point of a police desk -- a force's day
is mostly not offences -- but it was measured against the installed 3B model
and it does not hold: 50% correct with all twenty headings offered, and 60%
with only these five, which means the failure is the model's grasp of the
distinction rather than the size of the choice.

Newsroom language, though, is formulaic. "Transferred in a reshuffle",
"chargesheet filed", "challans issued", "prohibitory orders imposed" are stock
constructions that appear almost verbatim across papers and languages, and a
phrase table reads them exactly. So: rules where the wording is fixed, the
model where it is not.

The rules only ever REFINE a police answer. If the classifier said "sports"
this module is silent -- it has no opinion about anything outside the desk,
and overriding a heading it cannot check would trade one error for another.
Order matters within the table: an article that says both "arrested" and
"murder" is the arrest story, so investigation is tested before crime.

Hindi phrasings sit alongside the English because a Dainik Tribune page is
read by the same pipeline, and "गिरफ्तार" is as fixed a construction as
"arrested".
"""

import re

# The headings this module arbitrates between. Anything else is left alone.
DESK = ("crime", "investigation", "policing", "security", "traffic")

# Ordered. The first heading with a matching cue wins, so the sequence encodes
# which reading takes precedence when an article supports several -- an arrest
# for a murder is an arrest story, and a transfer of officers investigating a
# case is a posting story.
_RULES = [
    ("traffic", [
        r"\bchallan", r"\btraffic police\b", r"\bdrunk[en]?[ -]driving\b",
        r"\btraffic (?:violation|diversion|jam|rule)", r"\bovers?peeding\b",
        r"\broad safety\b", r"\bvehicle check", r"\bimpound",
        r"\bno[ -]parking\b", r"\bdriving licence\b", r"\bhelmet\b",
        r"चालान", r"यातायात पुलिस", r"नशे में गाड़ी",
    ]),
    ("policing", [
        r"\btransferr?ed\b", r"\breshuffle\b", r"\bposting", r"\bpromot(?:ed|ion)\b",
        r"\brecruit", r"\bgallantry\b", r"\bhonoured\b", r"\bmedal",
        r"\bpolice (?:reform|welfare|training|academy|infrastructure|budget)",
        r"\bnew police station\b", r"\bsuspended\b.{0,40}\b(?:cop|constable|officer|inspector)",
        r"\bdepartmental (?:inquiry|enquiry)\b", r"\bcustodial death\b",
        r"\bcops? (?:honoured|felicitated|awarded)", r"\bSHOs? transferred\b",
        r"\bpolice (?:turn|adopt|deploy|install|launch|introduce)s? \w+",
        r"तबादला", r"स्थानांतरण", r"पदोन्नति", r"भर्ती", r"सम्मानित",
    ]),
    ("investigation", [
        r"\barrest", r"\bheld\b.{0,30}\b(?:police|cops)\b", r"\bchargesheet\b",
        r"\bcharge sheet\b", r"\bFIR\b", r"\bprobe\b", r"\binvestigat",
        r"\braid(?:ed|s)?\b", r"\bremand", r"\bcustody\b", r"\bmanhunt\b",
        r"\bSIT\b", r"\bCBI\b", r"\bforensic", r"\bseiz(?:ed|ure)\b",
        r"\brecover(?:ed|y)\b", r"\bencounter\b", r"\bcyber cell\b",
        r"\bnabbed\b", r"\bbooked under\b", r"\bcrackdown\b",
        r"गिरफ्तार", r"चार्जशीट", r"छापेमारी", r"जांच", r"बरामद",
    ]),
    ("security", [
        r"\bdeploy", r"\bbandobast\b", r"\bpatrol", r"\bsection 144\b",
        r"\bprohibitory order", r"\bcurfew\b", r"\bVIP security\b",
        r"\bcommunal tension\b", r"\blaw and order\b", r"\bsecurity review\b",
        r"\bcheckpost\b", r"\bbarricad", r"\bparamilitary\b",
        r"\bflag march\b", r"\bhigh alert\b",
        r"तैनात", r"धारा 144", r"कर्फ्यू", r"सुरक्षा व्यवस्था",
    ]),
    ("crime", [
        r"\bmurder", r"\brobb(?:ed|ery)\b", r"\bloot(?:ed)?\b", r"\btheft\b",
        r"\bstolen\b", r"\bsnatch", r"\bburglar", r"\bdacoity\b",
        r"\brap(?:e|ed)\b", r"\bmolest", r"\bassault", r"\bkidnap",
        r"\bextortion\b", r"\bcheat(?:ed|ing)\b", r"\bfraud\b",
        r"\bmurdered\b", r"\bshot dead\b", r"\bstabbed\b",
        r"हत्या", r"लूट", r"चोरी", r"बलात्कार", r"अपहरण", r"ठगी",
    ]),
]

_COMPILED = [(head, [re.compile(p, re.IGNORECASE) for p in pats])
             for head, pats in _RULES]


def desk_heading(text: str) -> str:
    """
    The police heading this article's wording points to, or "".

    Returns the FIRST heading whose vocabulary appears, in the order above.
    That ordering is the whole rule: it is what makes "arrested for murder"
    an investigation story rather than a crime one, without needing to weigh
    how many cues each heading scored.
    """
    t = text or ""
    if not t.strip():
        return ""
    for head, pats in _COMPILED:
        if any(p.search(t) for p in pats):
            return head
    return ""


# Headlines that belong to another desk however much police vocabulary they
# also carry. A bail hearing names an arrest; a fatal crash names the police
# who attended. Both are cases where the police word is incidental and the
# other heading is the story, so these are checked BEFORE any desk cue and
# stop the rules from reaching in.
_NOT_OURS = [
    ("court", [r"\bcourt\b", r"\bhigh court\b", r"\bsupreme court\b",
               r"\bjudge\b", r"\bbench\b", r"\bverdict\b", r"\bbail\b",
               r"\bacquit", r"\bconvict(?:ed|ion)\b", r"\bhearing\b",
               r"\bstays?\b", r"\bpetition", r"\btribunal\b",
               r"अदालत", r"हाईकोर्ट", r"जमानत", r"फैसला"]),
    ("accident", [r"\bkilled as\b", r"\brams? into\b", r"\bcollision\b",
                  r"\bmishap\b", r"\bderail", r"\bdrown", r"\bfire broke out\b",
                  r"\bbuilding collapse", r"\bgas leak\b",
                  r"हादसा", r"दुर्घटना"]),
]

_NOT_OURS_C = [(head, [re.compile(p, re.IGNORECASE) for p in pats])
               for head, pats in _NOT_OURS]


def elsewhere(headline: str) -> str:
    """The non-desk heading this headline plainly belongs to, or ""."""
    for head, pats in _NOT_OURS_C:
        if any(p.search(headline or "") for p in pats):
            return head
    return ""


def refine(category: str, headline: str, body: str = "") -> str:
    """
    Correct a heading using the article's own wording.

    Two ways in, and the difference matters:

    The model answered WITHIN the desk (or not at all). Then the rules
    arbitrate freely, headline first and body as a fallback, because the only
    question left is which of the five it is.

    The model answered OUTSIDE the desk -- "technology", "court". Then only
    the HEADLINE can pull it back, and only when the headline is not itself
    plainly a court or accident story. A body mention is not enough: the body
    of a court report recounts the arrest at length, and letting that decide
    would refile every bail hearing as an investigation.
    """
    cat = (category or "").strip().lower()

    if cat and cat not in DESK:
        if elsewhere(headline):
            return category            # it is a court or accident story
        return desk_heading(headline) or category

    if elsewhere(headline) and not desk_heading(headline):
        # The model put it on the desk, but the headline says otherwise and
        # carries no desk cue of its own.
        return elsewhere(headline)
    return desk_heading(headline) or desk_heading(body) or category
