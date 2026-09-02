"""
Topic labelling: what every article is about, decided once when it is filed.

This is the stage that makes browsing by subject possible. It runs at ingest,
not at query time, and that placement is the whole point. The alternative --
asking a model at the moment somebody searches, which is what topics.py does --
re-reads and re-judges the same page for every question asked about it, and
costs minutes per question. Labelling once costs one call per fifteen articles
and makes every later question an indexed lookup.

The model is asked for the topic IN ITS OWN WORDS rather than made to choose
from a fixed list. That is a deliberate choice with a real trade-off:

  What it buys   The list reflects what the newspapers actually published. A
                 subject that starts appearing gets an entry without anybody
                 defining it first, and no taxonomy has to be designed, agreed
                 or maintained.

  What it costs  The same subject arrives under several names. That is handled
                 downstream -- catdata.normalise folds the mechanical variants
                 away and epaper/categories.py merges the rest once the counts
                 show which entries are really one.

Both the label the model gave and the entry it was filed under are kept. The
raw value is what the merge tooling reasons about, and throwing it away would
mean never being able to tell a bad alias from a bad classification.

The label is requested in English regardless of the language of the article.
A bilingual corpus labelled in each article's own language splits every subject
in two -- "crime" and "अपराध" as separate entries, neither complete. The
original wording still reaches search through `tags` and the body text.

Failure is non-fatal throughout. An article the model could not label is filed
under catdata.UNKNOWN rather than dropped, because an unlabelled article is a
gap in one facet count while a dropped article is a story that has left the
archive.
"""

from __future__ import annotations

import re

from . import catdata
from . import policedesk, vlm

# Ten rather than the twenty-five topics.py screens with. Screening asks for
# one bit per story and can afford a long list; this asks for a label and a
# keyword set per story, so the answer is roughly six times longer.
#
# Measured on qwen2.5vl:3b against real pages, asking for N articles at once:
#   6 -> 6 answered     8 -> 7 answered     10 -> 10 answered
#  12 -> 1 answered    15 -> 15 answered
# There is no size threshold in that; a small model simply stops early
# sometimes, and a bigger batch makes each lapse more expensive. Hence a
# moderate batch AND the retry below -- neither alone is enough.
BATCH = 10

# How many times a batch is re-asked for the articles it did not answer for.
# Two: once for the stragglers together, then once article-by-article. Bounded
# deliberately -- an unbounded retry against a model that has decided not to
# answer is an overnight job that never finishes.
RETRIES = 2

# Enough of each story for the subject to be unambiguous. A topic is almost
# always established in the headline and the first sentence or two; sending
# the whole body would multiply the prompt for no gain in accuracy.
OPENING = 420

# Built from catdata.PARENTS rather than typed out again, so the headings the
# model is offered cannot drift from the headings the code accepts. When they
# did drift, every off-list answer became a subcategory with no parent.
PROMPT = """You are filing newspaper articles in an archive with two levels.

For every numbered article below, give:

  "category"     The heading it files under. Choose EXACTLY ONE from this list
                 and use that spelling:
                 %s

  "subcategory"  The specific kind of thing within that heading, 1-2 words, in
                 ENGLISH, in your own words. Under crime: "murder", "robbery",
                 "cybercrime", "snatching". Under politics: "election",
                 "protest". Under accident: "road accident", "fire".
                 It must name the TYPE OF EVENT -- never a place, a person, an
                 organisation or a date. Return "" if the article does not fix
                 a specific kind.

  "tags"         2-5 lowercase keywords for everything else: the place, the
                 people, the organisations, the details. Places and names
                 belong HERE, never in "subcategory".

Answer in English even when the article is in Hindi. Label what the article
reports, not what it mentions in passing: a political speech about farming is
politics, a report on a failed harvest is agriculture.

Police stories split across five headings. Most of them are NOT "crime".
Decide by asking what the headline is announcing:

  crime          An offence, reported as the news. There is a victim and the
                 offence is what happened.
                 YES "Woman robbed at knifepoint in Sector 22"
                 NO  anything whose headline leads with an arrest or a probe

  investigation  The case being worked. If the headline announces an ARREST,
                 a chargesheet, an FIR, a raid, a recovery, a remand or a
                 probe, it is investigation -- even when it also names the
                 original offence.
                 YES "Man arrested for cheating 40 people of Rs 2 crore"
                 YES "1,500-page chargesheet filed in fire tragedy"

  policing       The police force itself as an organisation: transfers,
                 recruitment, promotions, medals, training, misconduct,
                 budgets, new stations, and any EQUIPMENT OR SYSTEM THE
                 POLICE ADOPT. Choose this even though the article mentions
                 criminals or crimes -- the subject is the force.
                 YES "34 cops honoured for gallantry"
                 YES "Police turn to facial recognition to track criminals"
                     (the subject is the police adopting it, not technology)

  security       Keeping public order in advance of something: bandobast,
                 deployment, patrolling, VIP security, curfew, Section 144,
                 communal tension.
                 YES "Heavy deployment ahead of Sunday rally"

  traffic        ENFORCEMENT of road rules only: challans, drunk-driving
                 drives, diversions, licences, parking, vehicle checks.
                 A CRASH IS NOT TRAFFIC. A collision in which people were
                 hurt or killed is "accident", however many vehicles.
                 YES "12,000 challans issued in a week"
                 NO  "Two killed as car rams into truck" -> that is accident

A court ruling, stay, verdict, bail or hearing is "court", not politics, even
when the case is about a government decision.

Do NOT write a sentence, and do not write a path like "crime/murder".

ARTICLES:
%%s

Return ONLY JSON:
{"items":[{"n":<number>,"category":"<heading>","subcategory":"<kind>",
"tags":["<keyword>"]}]}""" % ", ".join(catdata.PARENTS)


def _opening(a: dict) -> str:
    """Headline plus the start of the body -- the part that fixes the subject."""
    head = (a.get("headline") or "").strip()
    body = (a.get("body") or a.get("summary") or a.get("content") or "").strip()
    text = ("%s. %s" % (head, body)).strip(". ") if head else body
    return re.sub(r"\s+", " ", text)[:OPENING]


def _empty(reason: str = "") -> dict:
    return {"category": catdata.UNKNOWN, "subcategory": "", "raw_category": "",
            "tags": [], "classify_error": reason}


def _one(item: dict, article: dict = None) -> dict:
    """One model answer to the fields stored on an article."""
    def text(key):
        v = item.get(key)
        return v.strip() if isinstance(v, str) else ""

    # Both namings are accepted. PROMPT asks for "topic"/"kind"; earlier
    # revisions asked for "category"/"subcategory", and a half-finished
    # reclassify should not have to be thrown away because the wording moved.
    raw = text("topic") or text("category")
    parent = catdata.normalise(raw)
    child = catdata.normalise(text("kind") or text("subcategory"))

    # The model was told to choose a heading from the list and sometimes will
    # not. Rather than discard the answer, treat an off-list heading as what it
    # actually is -- a subcategory whose parent is not yet known -- and let the
    # tree pass place it. Dropping it would lose a correct reading of the
    # article over a formatting mistake.
    if parent and not catdata.is_parent(parent):
        child = child or parent
        parent = catdata.seed_parent(parent) or ""

    # A subcategory repeating its parent carries nothing.
    if child == parent:
        child = ""

    # The police desk's five headings, arbitrated by the article's own
    # wording. Measured against the installed 3B model: it separates them
    # 61% of the time and the rules take that to 94%, almost all of the gain
    # being stories it filed as "crime" that are postings, arrests or
    # challans. See epaper/policedesk.py -- the rules are silent outside
    # those five, so nothing else is touched.
    if article is not None:
        parent = policedesk.refine(parent, article.get("headline") or "",
                                   article.get("body") or "")
        if child == parent:
            child = ""

    return {
        "category": parent or catdata.UNKNOWN,
        "subcategory": child,
        # Exactly what the model said. Kept because the merge tooling reasons
        # about it, and because a wrong entry in the alias table is only
        # diagnosable if the input to that table survives.
        "raw_category": raw,
        "tags": catdata.normalise_tags(item.get("tags")),
    }


def classify(articles: list, progress=None) -> list:
    """
    A topic and keyword set for every article, aligned to the input list.

    Always returns one result per input article, in order, whatever the model
    does. Callers index into it positionally, so a short or reordered answer
    from the model must not shift the alignment -- the numbering is local to
    each batch and mapped back explicitly, the same guard topics.screen uses.

    `progress(done, total)` is called per batch, for the job UI.
    """
    out = [_empty("not attempted") for _ in articles]
    if not articles:
        return out

    # Articles with nothing to read cannot be labelled and must not be sent:
    # a batch of blanks returns confident nonsense, and it would be filed.
    usable = [i for i, a in enumerate(articles) if len(_opening(a)) >= 25]
    for i in set(range(len(articles))) - set(usable):
        out[i] = _empty("too little text to label")

    for start in range(0, len(usable), BATCH):
        chunk = usable[start:start + BATCH]

        # Whatever the model has not answered for yet. Asking again for only
        # these is what makes a lapse cost one extra call instead of ten
        # labels -- and because the retry is a smaller prompt, it is also the
        # ask the model is most likely to complete.
        pending, err = list(chunk), ""
        for attempt in range(RETRIES + 1):
            if not pending:
                break
            # Last attempt goes one article at a time. A model that has twice
            # declined to answer for a story in company will usually answer
            # for it alone, and by this point there are only a few left.
            groups = ([[i] for i in pending] if attempt == RETRIES
                      else [pending])
            for group in groups:
                try:
                    got = _ask(articles, group)
                except Exception as ex:
                    err = str(ex)[:90]
                    continue
                for i, res in got.items():
                    out[i] = res
            pending = [i for i in pending if out[i].get("classify_error")]

        # Still nothing after every retry. Recorded per article, and the two
        # reasons are kept apart: a model that answered but omitted this story
        # is a different problem from a model that could not be reached.
        for i in pending:
            out[i] = _empty("classification failed: %s" % err if err
                            else "model gave no answer after %d attempts"
                            % (RETRIES + 1))

        if progress:
            progress(min(start + BATCH, len(usable)), len(usable))

    return out


def _ask(articles: list, chunk: list) -> dict:
    """
    One model call for one group. Returns {article index: fields} for whatever
    came back, which may be fewer than were asked for.

    Numbering is local to the group and mapped back through `chunk`, so a
    model that renumbers or skips cannot shift which article a label lands on
    -- the same guard topics.screen uses, and the reason a dropped answer
    shows up as an unlabelled article rather than a mislabelled one.
    """
    listing = "\n\n".join("%d. %s" % (k + 1, _opening(articles[i]))
                          for k, i in enumerate(chunk))
    data = vlm.text_json(PROMPT % listing, max_tokens=1600)
    items = (data.get("items") if isinstance(data, dict) else data) or []
    if not isinstance(items, list):
        raise ValueError("expected a list of items")

    got, seen = {}, set()
    for it in items:
        if not isinstance(it, dict):
            continue
        n = str(it.get("n", "")).strip()
        if not n.isdigit():
            continue
        k = int(n) - 1
        # Out of range means the model numbered something that was not in this
        # group. Dropping it is right: the alternative is filing one article's
        # topic against another's.
        if 0 <= k < len(chunk) and k not in seen:
            seen.add(k)
            got[chunk[k]] = _one(it, articles[chunk[k]])
    return got


PARENT_PROMPT = """Each line below is a newspaper subject heading.

File each one under EXACTLY ONE of these categories, using that spelling:
%s

Choose by what the subject IS. "murder" is crime. "election" is politics.
"monsoon" is environment. Use "other" only when nothing else is defensible.

SUBJECTS:
%%s

Return ONLY JSON:
{"items":[{"subject":"<subject as given>","category":"<category>"}]}""" % (
    ", ".join(catdata.PARENTS))

PARENT_BATCH = 25


def assign_parents(labels, progress=None) -> dict:
    """
    Put subcategories under headings. Returns {label: category}.

    Called once per LABEL, not once per article, which is what makes a
    two-level scheme affordable: an archive with 200 distinct subcategories
    and 50,000 articles costs eight model calls, not fifty thousand. The
    answers are stored on the category rows, so the cost is paid once and
    again only when a genuinely new subject appears.

    catdata.SEED_PARENTS answers the obvious ones for free and they are never
    sent. A model asked whether "murder" is crime will say yes, and paying for
    that is waste; the ones worth asking about are the subjects nobody
    anticipated, which is exactly the set free labelling produces.

    Fails open: a label that cannot be placed comes back "", and the caller
    leaves it unparented rather than guessing. An unparented subcategory shows
    at the top level, which is untidy; a wrongly parented one is filed where
    nobody will look for it.
    """
    out, ask = {}, []
    for raw in labels:
        lab = catdata.normalise(raw)
        if not lab or lab in out:
            continue
        if catdata.is_parent(lab):
            out[lab] = ""            # it IS a heading; it has no parent
            continue
        seed = catdata.seed_parent(lab)
        if seed:
            out[lab] = seed
        else:
            ask.append(lab)

    for start in range(0, len(ask), PARENT_BATCH):
        chunk = ask[start:start + PARENT_BATCH]
        listing = "\n".join(chunk)
        try:
            data = vlm.text_json(PARENT_PROMPT % listing, max_tokens=1200)
            items = (data.get("items") if isinstance(data, dict) else data) or []
            got = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                subj = catdata.normalise(it.get("subject") or "")
                cat = catdata.normalise(it.get("category") or "")
                # Only a real heading is accepted. A model inventing a parent
                # would put a heading in the picker that the reader has never
                # seen and that nothing else files under -- the drift the
                # controlled top level exists to prevent.
                if subj and catdata.is_parent(cat):
                    got[subj] = cat
            for lab in chunk:
                out[lab] = got.get(lab, "")
        except Exception:
            for lab in chunk:
                out[lab] = ""
        if progress:
            progress(min(start + PARENT_BATCH, len(ask)), len(ask))
    return out


PARENT_PROMPT = """Each line below is a subject label from a newspaper archive.

Put every one under exactly ONE of these headings:
%s

Judge what kind of story carries that label. "otp fraud" is crime, "by-election"
is politics, "dengue outbreak" is health. Use "other" only when nothing fits.

LABELS:
%s

Return ONLY JSON:
{"items":[{"label":"<the label, copied exactly>","category":"<heading>"}]}"""

# Labels per call. Larger than BATCH because each answer is two short strings
# rather than a label plus a keyword set, and because the whole point of doing
# this over labels is that there are tens of them, not thousands.
PARENT_BATCH = 25


def assign_parents(labels: list, progress=None) -> dict:
    """
    {label: parent heading} for a list of subcategory labels.

    The cheap half of building a two-level tree. An archive holds thousands of
    articles and tens of distinct labels, so the parent of "otp fraud" is asked
    once and applies to every article carrying it -- for the corpus this was
    written against, 15 labels rather than 133 articles, and the ratio only
    improves as the archive grows.

    Anything catdata already knows is answered for free and never sent. Labels
    the model declines to place are simply absent from the result: an unplaced
    label keeps working as a label, it just has no parent yet, and inventing
    one would be worse than leaving the gap visible.
    """
    out, ask = {}, []
    for lab in labels:
        norm = catdata.normalise(lab)
        if not norm or catdata.is_parent(norm):
            continue
        seeded = catdata.seed_parent(norm)
        if seeded:
            out[norm] = seeded
        else:
            ask.append(norm)

    heads = ", ".join(catdata.PARENTS)
    for start in range(0, len(ask), PARENT_BATCH):
        chunk = ask[start:start + PARENT_BATCH]
        try:
            data = vlm.text_json(
                PARENT_PROMPT % (heads, "\n".join(chunk)), max_tokens=900)
            for it in (data.get("items") if isinstance(data, dict) else data) or []:
                if not isinstance(it, dict):
                    continue
                lab = catdata.normalise(it.get("label"))
                par = catdata.normalise(it.get("category"))
                # Only a heading from the list, and only for a label actually
                # sent. A model inventing a parent would put a heading in the
                # picker that no article can ever be filed under.
                if lab in chunk and catdata.is_parent(par):
                    out[lab] = par
        except Exception as ex:
            print("assign_parents: batch failed: %s" % str(ex)[:110])
        if progress:
            progress(min(start + PARENT_BATCH, len(ask)), len(ask))
    return out


def summarise(results: list) -> dict:
    """Counts per topic for one run, for the job log and a quick eyeball."""
    by = {}
    failed = 0
    for r in results:
        by[r["category"]] = by.get(r["category"], 0) + 1
        if r.get("classify_error"):
            failed += 1
    return {"labelled": len(results) - failed, "failed": failed,
            "topics": dict(sorted(by.items(), key=lambda kv: -kv[1]))}
