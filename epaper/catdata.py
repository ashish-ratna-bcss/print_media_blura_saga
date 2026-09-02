"""
Topic-label normalisation: many spellings of one subject, one entry in the list.

The classifier is asked for the topic in its own words rather than picked from
a fixed list, so the reader browses what the newspapers actually published
instead of what somebody guessed in advance. The cost of that freedom is
drift: a model naming a subject freely will write "Cybercrime" one day,
"Cyber Crime" the next and "cyber-crime" the day after, and a picker holding
all three as separate entries is worse than a picker with a fixed list --
each one returns a third of the stories and none of them says so.

This module is the layer that stops that reaching the reader. Two mechanisms,
in order of how much they are trusted:

  NORMALISE  Mechanical and total. Case, spacing, punctuation and a handful of
             grammatical endings are folded away. It cannot be wrong, because
             it only removes distinctions that carry no meaning: "Cyber Crime",
             "cyber-crime" and "CYBERCRIME" are the same six letters.

  ALIAS      A judgement, applied only where two genuinely different words name
             one subject -- "law and order" is policing, "अपराध" is crime.
             Small on purpose and listed explicitly below, because every entry
             is a decision that a reader could disagree with, and a table that
             grows by guessing is how a topic quietly disappears from the list.

What is NOT here is a list of permitted topics. A label the model invents and
that matches no alias passes through normalised and is filed as it stands; the
merge tooling in epaper/categories.py is how two entries become one after the
fact, once the frequency counts show they are the same subject. Deciding in
advance is the thing this design set out to avoid.

Nothing here imports the rest of the package, touches a database or calls a
model. Normalisation runs on every article ingested, so it has to be free.
"""

from __future__ import annotations

import re
import unicodedata

# Devanagari, so a Hindi label is folded rather than stripped to nothing. The
# classifier is asked for English, but a 7B model asked for one language while
# reading another does not always comply, and a label reduced to "" by an
# over-eager regex is a story with no topic at all.
_DEVANAGARI = r"ऀ-ॿ"

# Anything that is not a letter, digit, space or Devanagari. Hyphens and
# ampersands included: "cyber-crime" and "cyber crime" are one subject, and
# "law & order" and "law and order" are too (the ampersand is expanded first).
_STRIP = re.compile(r"[^\w\s%s]+" % _DEVANAGARI, re.UNICODE)
_SPACE = re.compile(r"\s+")

# Trailing grammatical noise a model adds when it is describing rather than
# labelling: "crime news", "politics section", "sports related".
_SUFFIXES = ("news", "story", "stories", "article", "articles", "section",
             "related", "affairs", "reporting", "coverage", "beat", "desk")

# Leading articles, for the same reason: "the crime", "a robbery".
_PREFIXES = ("the", "a", "an")

# The judgement half. Left of the arrow is what a model has been seen to write
# or plausibly will; right is the entry it is filed under. Kept deliberately
# short -- these are decisions, not mechanics, and the merge tooling exists so
# that this table does not have to anticipate everything.
#
# The Hindi entries are here because the classifier is asked for an English
# label but reads Hindi copy, and compliance is not guaranteed. They are
# translations, not opinions, which is why they are safe to hard-code.
ALIASES = {
    # spelling and word-order variants that survive normalisation
    "cyber crime": "cybercrime",
    "cyber fraud": "cybercrime",
    "online fraud": "cybercrime",
    "internet crime": "cybercrime",
    "digital fraud": "cybercrime",
    "crime and law": "crime",
    "criminal": "crime",
    # These three predate the police-desk split and used to fold onto
    # "crime". "policing" is now a heading of its own, so that alias was
    # rewriting the classifier's correct answer into the wrong one before
    # anything downstream saw it -- which is most of why policing looked
    # unlearnable. "law and order" is what a paper calls deployment and
    # bandobast, which is security; a bare "police" is the force.
    "law and order": "security",
    "police": "policing",
    "police department": "policing",
    "road accident": "accident",
    "traffic accident": "accident",
    "accidents": "accident",
    "judiciary": "court",
    "courts": "court",
    "legal": "court",
    "judicial": "court",
    # Observed: qwen2.5vl:3b labels the same run of high-court stories "court"
    # and "justice" within one batch. Two words, one subject, and precisely the
    # kind of split the free-label design accepts in exchange for not having to
    # design a taxonomy -- resolved here rather than left to the reader.
    "justice": "court",
    "politics and government": "politics",
    "political": "politics",
    "government": "politics",
    "governance": "politics",
    "civic issues": "civic",
    "municipal": "civic",
    "local government": "civic",
    "economy": "business",
    "economic": "business",
    "finance": "business",
    "commerce": "business",
    "sport": "sports",
    "healthcare": "health",
    "medical": "health",
    "educational": "education",
    "schools": "education",
    "environmental": "environment",
    "climate": "environment",
    "agricultural": "agriculture",
    "farming": "agriculture",
    "international": "world",
    "foreign": "world",
    "global": "world",

    # हिन्दी -> English, so one subject is not split across two scripts
    "अपराध": "crime",
    "अपराधी": "crime",
    "पुलिस": "policing",
    "हत्या": "crime",
    "चोरी": "crime",
    "साइबर अपराध": "cybercrime",
    "साइबर ठगी": "cybercrime",
    "साइबर": "cybercrime",
    "राजनीति": "politics",
    "राजनीतिक": "politics",
    "सरकार": "politics",
    "अदालत": "court",
    "कोर्ट": "court",
    "न्यायालय": "court",
    "दुर्घटना": "accident",
    "हादसा": "accident",
    "व्यापार": "business",
    "कारोबार": "business",
    "अर्थव्यवस्था": "business",
    "खेल": "sports",
    "स्वास्थ्य": "health",
    "शिक्षा": "education",
    "मौसम": "weather",
    "कृषि": "agriculture",
    "पर्यावरण": "environment",
    "मनोरंजन": "entertainment",
    "विदेश": "world",
}

# ----------------------------------------------------------- the top level

# The parent categories. THIS list is controlled, and the subcategories under
# it are not -- which is the whole shape of the design.
#
# The reason for the asymmetry is what each level is for. The parent level is
# the picker: a reader opens a day and must see a handful of headings they
# recognise, in the same order, every time. A picker whose top level drifts is
# unusable, and free labelling drifts -- "crime" one day, "violent crime" the
# next, both half-populated. The child level is where the detail lives, it is
# searched rather than scanned, and there the drift is worth the coverage
# because the merge tooling can absorb it.
#
# Kept small on purpose. Fifteen headings fit on a screen; forty is a list
# nobody reads, and the second level exists precisely so this one can stay
# short.
PARENTS = [
    # The police desk's own headings, first because this archive is built for
    # a police beat and "crime" alone cannot carry it. A force's day is mostly
    # not offences: it is postings, recruitment, bandobast, challans, probes
    # and chargesheets, and filing all of that under "crime" produced an
    # archive where two thirds of the police coverage was indistinguishable.
    #
    # The four are separated by WHO the article is about and WHAT STAGE it
    # reports, which is the distinction a reporter already makes:
    #
    #   crime          an offence happened          -- victim and accused
    #   investigation  the case is being worked     -- probe, chargesheet, SIT
    #   policing       the force itself             -- transfers, recruitment
    #   security       order is being kept          -- bandobast, patrolling
    #   traffic        the road is being policed    -- challans, road safety
    "crime", "investigation", "policing", "security", "traffic",
    "court",
    "politics", "civic", "business", "education", "health",
    "sports", "entertainment", "accident", "environment", "agriculture",
    "technology", "religion", "world", "other",
]

# The headings above that belong to the police desk. Not used for filing --
# every parent is equal to the classifier -- but it is what lets the UI group
# the beat's own subjects together and what a report means by "police
# coverage" rather than "everything we indexed".
POLICE_PARENTS = ["crime", "investigation", "policing", "security", "traffic"]

# One line each, in the words a reader would use rather than the words the
# classifier was given. Shown in the Categories tab so somebody deciding where
# an article belongs can see the whole scheme instead of inferring it from
# whatever labels happen to have been used so far.
#
# Kept here beside PARENTS rather than in the frontend: a heading added to the
# list and not described is then visibly missing a description, instead of
# silently appearing in one place and not the other.
PARENT_NOTES = {
    "crime": "An offence happened — there is a victim.",
    "investigation": "The case being worked: arrests, chargesheets, raids, probes.",
    "policing": "The force itself: transfers, recruitment, medals, equipment.",
    "security": "Keeping order: deployment, bandobast, curfew, Section 144.",
    "traffic": "Road-rule enforcement: challans, drunk driving, diversions.",
    "court": "Rulings, verdicts, bail, hearings, petitions.",
    "politics": "Parties, elections, government, protests, statements.",
    "civic": "Roads, water, power, sanitation, municipal services.",
    "business": "Companies, markets, banking, trade, the economy.",
    "education": "Schools, colleges, exams, admissions, teachers.",
    "health": "Hospitals, disease, treatment, public health.",
    "sports": "Matches, tournaments, players, results.",
    "entertainment": "Cinema, music, television, celebrity.",
    "accident": "Crashes, fires, drownings, collapses — nobody meant it.",
    "environment": "Weather, pollution, floods, wildlife.",
    "agriculture": "Farming, crops, farmers, the harvest.",
    "technology": "Products, internet, science, digital services.",
    "religion": "Temples, festivals, rituals, religious bodies.",
    "world": "Other countries, diplomacy, foreign affairs.",
    "other": "Real stories that fit nowhere above — obituaries, anniversaries.",
}


def children_of(parent: str) -> list:
    """
    The seeded subcategories that file under a heading, sorted.

    Only the ones written down in SEED_PARENTS. Subcategories the classifier
    invents are not here by design -- they are in the database, counted, and
    listed by the registry. This answers "what did we decide in advance",
    which is a different question from "what has been used".
    """
    # `c != parent` guards a self-mapping. Those do nothing -- seed_parent()
    # already returns "" for anything that IS a heading -- but left in they
    # would render "traffic" as a subcategory of traffic in the panel.
    return sorted(c for c, p in SEED_PARENTS.items()
                  if p == parent and c != parent)

# Children whose parent is not worth asking a model about, because there is
# only one sane answer and paying for it once per label per rebuild is waste.
# Everything absent from here is assigned by epaper/classify.assign_parents.
SEED_PARENTS = {
    # -- offences: something was done to somebody --------------------------
    "murder": "crime", "robbery": "crime", "theft": "crime",
    "snatching": "crime", "burglary": "crime", "dacoity": "crime",
    "cybercrime": "crime", "fraud": "crime", "kidnapping": "crime",
    "assault": "crime", "smuggling": "crime", "trafficking": "crime",
    "rape": "crime", "extortion": "crime", "arson": "crime",
    "riot": "crime", "gangster": "crime", "narcotics": "crime",
    "molestation": "crime", "dowry": "crime", "domestic violence": "crime",
    "loot": "crime", "chain snatching": "crime", "vandalism": "crime",
    "counterfeit": "crime", "human trafficking": "crime",
    "drug seizure": "crime", "liquor seizure": "crime",
    "attempt to murder": "crime", "abduction": "crime", "suicide": "crime",

    # -- the case being worked ---------------------------------------------
    # "arrest" sits here rather than under crime on purpose. An arrest is not
    # an offence; it is the investigation reaching somebody, and a police desk
    # searching for arrests wants the case, not the original crime report.
    "arrest": "investigation", "chargesheet": "investigation",
    "probe": "investigation",
    "sit": "investigation", "fir": "investigation", "raid": "investigation",
    "forensics": "investigation", "interrogation": "investigation",
    "manhunt": "investigation", "remand": "investigation",
    "seizure": "investigation", "recovery": "investigation",
    "encounter": "investigation", "custody": "investigation",
    "cbi probe": "investigation", "narcotics bust": "investigation",
    "crackdown": "investigation", "sting": "investigation",

    # -- the force as an institution ---------------------------------------
    "police": "policing", "transfer": "policing", "posting": "policing",
    "recruitment": "policing", "promotion": "policing",
    "training": "policing", "gallantry award": "policing",
    "police welfare": "policing", "police station": "policing",
    "police reform": "policing", "custodial death": "policing",
    "police misconduct": "policing", "suspension": "policing",
    "departmental inquiry": "policing", "police infrastructure": "policing",
    "helpline": "policing", "community policing": "policing",
    "cyber cell": "policing", "women police": "policing",
    "facial recognition": "policing", "surveillance": "policing",

    # -- keeping order -----------------------------------------------------
    "bandobast": "security", "patrolling": "security",
    "vip security": "security", "curfew": "security",
    "communal tension": "security", "law and order": "security",
    "prohibitory orders": "security", "section 144": "security",
    "border security": "security", "terrorism": "security",
    "naxal": "security", "insurgency": "security", "militancy": "security",
    "security review": "security", "deployment": "security",
    "checkpost": "security", "curfew relaxation": "security",

    # -- the road ----------------------------------------------------------
    "challan": "traffic", "traffic violation": "traffic",
    "drunk driving": "traffic", "road safety": "traffic",
    "traffic diversion": "traffic", "parking": "traffic",
    "overspeeding": "traffic", "licence": "traffic",
    "vehicle check": "traffic", "traffic jam": "traffic",

    "election": "politics", "parliament": "politics", "assembly": "politics",
    "protest": "politics", "diplomacy": "world", "governance": "politics",

    "judiciary": "court", "verdict": "court", "bail": "court",
    "high court": "court", "supreme court": "court",

    "infrastructure": "civic", "roadworks": "civic", "sanitation": "civic",
    "water supply": "civic", "electricity": "civic", "municipal": "civic",
    "transport": "civic", "housing": "civic",

    "economy": "business", "market": "business", "banking": "business",
    "startup": "business", "trade": "business",

    "cricket": "sports", "football": "sports", "hockey": "sports",
    "olympics": "sports",

    "cinema": "entertainment", "music": "entertainment",
    "television": "entertainment", "celebrity": "entertainment",

    "road accident": "accident", "fire": "accident", "drowning": "accident",
    "collapse": "accident",

    "weather": "environment", "pollution": "environment",
    "flood": "environment", "wildlife": "environment",

    "farming": "agriculture", "crops": "agriculture", "monsoon": "environment",

    "obituary": "other", "celebration": "other", "history": "other",
    "anniversary": "other", "advertisement": "other",
}


def is_parent(label: str) -> bool:
    return (label or "") in PARENTS


def seed_parent(label: str) -> str:
    """
    The parent for a child label where one is obvious. "" when it is not --
    the caller then asks a model, once per label rather than once per article.
    """
    lab = normalise(label)
    if not lab:
        return ""
    if lab in PARENTS:
        return ""            # it IS a parent; it has none
    return SEED_PARENTS.get(lab, "")


# What an article is filed under when the classifier returned nothing usable.
# A named value rather than "" so that "the model gave no answer" is visible in
# the facet counts instead of looking like an article nobody has got to yet.
UNKNOWN = "unclassified"

# Longer than any real topic. A model that returns a sentence has described the
# article instead of labelling it, and filing that sentence would put a
# one-article entry in the picker forever.
MAX_LABEL = 40

# A character limit alone cannot tell a label from a short sentence: "this is a
# whole sentence about something" is exactly forty characters and passed the
# length check unchallenged. Labels are asked for as one to three words, so the
# count of words is what actually separates them; four allows a little slack
# ("road traffic accident case") without admitting prose.
MAX_WORDS = 4


def normalise(label: str) -> str:
    """
    A model's topic label to the entry it is filed under.

    Idempotent: normalise(normalise(x)) == normalise(x), which matters because
    stored labels are re-normalised whenever the alias table changes and a
    second pass must not keep shifting them.

    Returns "" for anything unusable, so the caller decides what an absent
    label means rather than having UNKNOWN forced on it here.
    """
    if not label or not isinstance(label, str):
        return ""

    # NFC first: Devanagari composes several ways and two byte sequences that
    # render identically must not become two entries in the picker.
    t = unicodedata.normalize("NFC", label).strip().lower()
    t = t.replace("&", " and ")
    t = _STRIP.sub(" ", t)
    t = _SPACE.sub(" ", t).strip()
    if not t:
        return ""

    # Strip the framing words a model wraps a label in, repeatedly -- "the
    # crime news section" is three of them around one real word.
    changed = True
    while changed and t:
        changed = False
        for p in _PREFIXES:
            if t.startswith(p + " "):
                t, changed = t[len(p) + 1:].strip(), True
        for s in _SUFFIXES:
            if t.endswith(" " + s):
                t, changed = t[:-(len(s) + 1)].strip(), True

    if not t or len(t) > MAX_LABEL or len(t.split()) > MAX_WORDS:
        return ""

    # Aliases last, so they are matched against the cleaned form and the table
    # does not need an entry per spelling. Applied once and not chased: a
    # chain a -> b -> c is a mistake in the table, and following it would hide
    # that mistake rather than surface it.
    return ALIASES.get(t, t)


def normalise_tags(tags, limit: int = 8) -> list:
    """
    The keyword list beside the topic. Same folding, order preserved.

    Tags are not aliased -- they are the specifics that make search sharp, and
    collapsing "otp fraud" into "cybercrime" would throw away the distinction
    they exist to carry. Only mechanical cleaning applies.
    """
    if not isinstance(tags, (list, tuple)):
        tags = [tags] if tags else []
    out = []
    for t in tags:
        if not isinstance(t, str):
            continue
        v = unicodedata.normalize("NFC", t).strip().lower()
        v = _SPACE.sub(" ", _STRIP.sub(" ", v.replace("&", " and "))).strip()
        if v and len(v) <= MAX_LABEL and v not in out:
            out.append(v)
    return out[:limit]


def display(label: str) -> str:
    """
    The filed entry as a reader should see it.

    Title case, except for the acronyms a newspaper actually uses -- "FIR" and
    "GST" are not "Fir" and "Gst". Purely presentational; the stored value is
    always the normalised one.
    """
    if not label:
        return ""
    acronyms = {"fir", "gst", "cbi", "ed", "sit", "ncr", "un", "rti", "bjp",
                "sc", "hc", "cctv", "atm", "otp", "gdp", "ipo", "ai"}
    return " ".join(w.upper() if w in acronyms else w.capitalize()
                    for w in label.split())
