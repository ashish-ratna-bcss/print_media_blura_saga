"""
Topic filtering: pull only the stories about one subject out of a whole edition.

Two stages, because the cost of the two questions is wildly different.

  SCREEN   A cheap text-model pass over every story's opening text, in batches,
           answering only "could this plausibly be about the topic?". Tuned to
           over-include: a story wrongly dropped here is gone for good, while a
           story wrongly kept costs one assess call.

  ASSESS   The survivors go one at a time to the text model, which decides
           relevance properly and says why. This is where precision comes from.

Screening first matters. A front section can hold 60 stories; assessing all of
them costs 60 calls, while screening 60 headlines costs two and usually leaves
under ten to assess.

Both stages run on the text model (qwen2.5:7b by default), not the vision one.
Deciding whether a story is about policing does not require looking at the page.
"""

from __future__ import annotations

import json
import re

from . import vlm

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


def screen(articles: list, topic: str, progress=None) -> list:
    """Indices of articles worth assessing. Fails open: on error, keep all."""
    keep = []
    for start in range(0, len(articles), SCREEN_BATCH):
        chunk = articles[start:start + SCREEN_BATCH]
        listing = "\n".join("%d. %s" % (start + i + 1, _opening(a))
                            for i, a in enumerate(chunk))
        try:
            out = vlm.text_json(SCREEN_PROMPT % (topic, listing), max_tokens=600)
            nums = out.get("keep") or []
            keep += [int(n) - 1 for n in nums
                     if isinstance(n, (int, str)) and str(n).strip().isdigit()]
        except Exception:
            # A screening failure must not silently discard a whole batch.
            keep += list(range(start, start + len(chunk)))
        if progress:
            progress(min(start + SCREEN_BATCH, len(articles)), len(articles))
    return sorted({i for i in keep if 0 <= i < len(articles)})


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
         progress=None) -> dict:
    """
    Full filter over one edition's articles.

    Returns matches sorted by score, plus counts so the UI can show how much
    was searched rather than only what survived.
    """
    if not articles:
        return {"topic": topic, "searched": 0, "screened": 0, "matches": []}

    if progress:
        progress("screening", 0, len(articles))
    cand = screen(articles, topic,
                  progress=(lambda d, t: progress("screening", d, t)) if progress else None)

    matches = []
    for n, i in enumerate(cand, 1):
        if progress:
            progress("assessing", n, len(cand))
        a = articles[i]
        v = assess(a, topic)
        if v["relevant"] and v["score"] >= min_score:
            matches.append({**a, "topic_score": round(v["score"], 2),
                            "topic_why": v["why"], "topic_summary": v["summary"],
                            "entities": v["entities"]})
    matches.sort(key=lambda m: -m["topic_score"])
    return {"topic": topic, "searched": len(articles),
            "screened": len(cand), "matches": matches}
