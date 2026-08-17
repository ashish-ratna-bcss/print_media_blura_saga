"""
Topic filtering: pull only the stories about one subject out of a whole edition.

Three stages now, cheapest first, because the cost of the questions is wildly
different and most stories can be settled by the cheap ones.

  PREFILTER  Free. keywords.py scores every story against a hand-built beat
             vocabulary and drops only those that matched nothing at all while
             having enough text for that to mean something. No model involved.

  SCREEN     A cheap text-model pass over the survivors' opening text, in
             batches, answering only "could this plausibly be about the topic?".
             Tuned to over-include: a story wrongly dropped here is gone for
             good, while a story wrongly kept costs one assess call.

  ASSESS     The survivors go one at a time to the text model, which decides
             relevance properly and says why. This is where precision comes from.

Ordering matters. A front section can hold 60 stories; assessing all of them
costs 60 calls, while the prefilter is free, screening what is left costs two,
and under ten usually reach assess.

The two model stages run on the text model (qwen2.5:7b by default), not the
vision one. Deciding whether a story is about policing does not require looking
at the page -- and with a resolved beat, often does not require a model at all
(see mode="keywords" in find()).
"""

from __future__ import annotations

import json
import re

from . import keywords, vlm

SCREEN_BATCH = 25

SCREEN_PROMPT = """You are filtering newspaper stories for one topic.

TOPIC: %s

Below are numbered story openings. For each, decide whether it could plausibly
be about that topic. Judge the subject matter, not individual words -- a story
about an arrest is about policing even if the word "police" never appears.

Be INCLUSIVE at this stage. If it might be relevant, keep it; a later step
checks properly. Only drop stories that are clearly about something else.

STORIES:
%s

Return ONLY JSON, listing the numbers you are keeping:
{"keep":[<numbers>]}"""

ASSESS_PROMPT = """Decide whether one newspaper story is about a topic.

TOPIC: %s

STORY:
%s

Answer strictly about THIS story. Do not reward keyword overlap: a story that
merely mentions the topic in passing is not about it. Reply in the story's own
language for "summary", and in English for "why".

Return ONLY JSON:
{"relevant":<true|false>,"score":<0.0-1.0>,
 "why":"<one short sentence>","summary":"<one or two sentences>",
 "entities":["<people, places or organisations named>"]}"""


def _opening(a, limit=340):
    """Best available text for screening: body, else summary, else headline."""
    for k in ("body", "summary", "content", "headline"):
        v = (a.get(k) or "").strip()
        if len(v) > 20:
            return re.sub(r"\s+", " ", v)[:limit]
    return re.sub(r"\s+", " ", " ".join(
        str(a.get(k) or "") for k in ("headline", "summary", "body")))[:limit]


def screen(articles: list, topic: str, progress=None, spec=None,
           prefilter: bool = True) -> list:
    """
    Indices of articles worth assessing. Fails open: on error, keep all.

    The free keyword prefilter runs first unless disabled, so the model only
    sees stories with at least some lexical evidence. Batches are numbered
    locally and mapped back through `cand`, so a model that renumbers or skips
    cannot silently shift which article a number refers to.
    """
    spec = keywords.expand(topic) if spec is None else spec
    cand = (keywords.prefilter(articles, spec)["keep"] if prefilter
            else list(range(len(articles))))

    keep = []
    for start in range(0, len(cand), SCREEN_BATCH):
        chunk = cand[start:start + SCREEN_BATCH]
        listing = "\n".join("%d. %s" % (k + 1, _opening(articles[i]))
                            for k, i in enumerate(chunk))
        try:
            out = vlm.text_json(SCREEN_PROMPT % (topic, listing), max_tokens=600)
            for n in (out.get("keep") or []):
                if isinstance(n, (int, str)) and str(n).strip().isdigit():
                    k = int(str(n).strip()) - 1
                    if 0 <= k < len(chunk):
                        keep.append(chunk[k])
        except Exception:
            # A screening failure must not silently discard a whole batch.
            keep += chunk
        if progress:
            progress(min(start + SCREEN_BATCH, len(cand)), len(cand))
    return sorted(set(keep))


def assess(article: dict, topic: str) -> dict:
    """Proper relevance judgement for one story."""
    text = "\n".join(x for x in [
        (article.get("headline") or "").strip(),
        (article.get("byline") or "").strip(),
        (article.get("body") or article.get("summary") or "").strip()[:2200],
    ] if x)
    if len(text) < 25:
        return {"relevant": False, "score": 0.0, "why": "too little text to judge",
                "summary": "", "entities": []}
    try:
        d = vlm.text_json(ASSESS_PROMPT % (topic, text), max_tokens=700)
    except Exception as ex:
        return {"relevant": False, "score": 0.0,
                "why": "assessment failed: %s" % str(ex)[:90],
                "summary": "", "entities": []}
    ents = d.get("entities") or []
    if not isinstance(ents, list):
        ents = [str(ents)]
    return {"relevant": bool(d.get("relevant")),
            "score": float(d.get("score") or 0),
            "why": (d.get("why") or "").strip(),
            "summary": (d.get("summary") or "").strip(),
            "entities": [str(e) for e in ents][:8]}


def find(articles: list, topic: str, min_score: float = 0.5,
         progress=None, mode: str = "hybrid") -> dict:
    """
    Full filter over one edition's articles.

    mode="hybrid"    keyword prefilter, then model screen, then model assess.
                     Slow and precise; the default.
    mode="keywords"  lexical only, no model call at all. Seconds instead of
                     minutes, and it still works when the model server is down,
                     but it cannot tell a police story from a story quoting a
                     policeman. Only meaningful for a resolved beat -- an
                     unexpanded free-text query has no vocabulary to match.

    Returns matches sorted by score, plus counts so the UI can show how much was
    searched rather than only what survived.
    """
    spec = keywords.expand(topic)
    base = {"topic": topic, "mode": mode, "beat": spec["beat"],
            "expanded": spec["expanded"], "terms": spec["terms"]}
    if not articles:
        return {**base, "searched": 0, "screened": 0, "matches": []}

    if mode == "keywords":
        matches = keywords.rank(articles, spec, min_score)
        return {**base, "searched": len(articles),
                "screened": len(articles), "matches": matches}

    if progress:
        progress("screening", 0, len(articles))
    cand = screen(articles, topic, spec=spec,
                  progress=(lambda d, t: progress("screening", d, t)) if progress else None)

    matches = []
    for n, i in enumerate(cand, 1):
        if progress:
            progress("assessing", n, len(cand))
        a = articles[i]
        v = assess(a, topic)
        # The beat's geography applies to the model's verdict as well; see
        # keywords.adjust for why this cannot live inside keywords.score alone.
        adj = keywords.adjust(v["score"], a, spec)
        if v["relevant"] and adj["score"] >= min_score:
            why = v["why"]
            if adj["reason"]:
                why = "%s [scored down: %s]" % (why, adj["reason"])
            matches.append({**a, "topic_score": adj["score"],
                            "topic_why": why, "topic_summary": v["summary"],
                            "entities": v["entities"],
                            "keywords": adj["hits"]})
    matches.sort(key=lambda m: -m["topic_score"])
    return {**base, "searched": len(articles),
            "screened": len(cand), "matches": matches}
