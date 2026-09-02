"""
Storage and the browse path.

Two things are defended here. First, that `category` and `primary_beat` stay
apart -- they answer different questions, they lived under one name once, and
a corpus of police beats answering every question about subject matter with
"delhi-police" is the bug this whole change exists to remove.

Second, that the browse journey a reader actually takes -- a date, then the
topics on it, then the clippings -- returns consistent numbers at every step.
"""

from __future__ import annotations

import pytest

from conftest import needs_mongo

from epaper import catdata, categories
from epaper.corpus import Corpus


@pytest.fixture
def corpus(scratch_db, monkeypatch):
    c = Corpus(db=scratch_db, quiet=True)
    # Point the process-wide registry at the same scratch database, so
    # ingest's call to categories.file_as does not touch the real one.
    monkeypatch.setattr(categories, "_store",
                        categories.CategoryStore(db=scratch_db))
    return c


def art(seq, headline, body, page=1, category="", tags=(), top=0):
    return {"seq": seq, "type": "article", "headline": headline, "body": body,
            "page_ref": page, "bbox": [0, top, 100, top + 50],
            "category": category, "raw_category": category,
            "tags": list(tags)}


@needs_mongo
def test_topic_and_beat_are_stored_separately(corpus):
    corpus.ingest([art(1, "Two held for Rohini snatching",
                       "Delhi Police arrested two men after a chain snatching "
                       "near sector 7 on Monday evening.",
                       category="Crime", tags=["snatching"])],
                  job_id="j", paper="P", date="2026-08-30")
    d = corpus.articles.find_one({})
    assert d["category"] == "crime"           # what it is ABOUT
    assert d["primary_beat"] == "delhi-police"  # where it BELONGS
    assert d["raw_category"] == "Crime"
    assert d["tags"] == ["snatching"]


@needs_mongo
def test_never_classified_and_unclassifiable_are_distinguishable(corpus):
    """
    "" means nobody has run the classifier. catdata.UNKNOWN means it ran and
    could not label the article. Collapsing them makes a model outage look
    like a backlog, and only one of those is fixed by waiting.
    """
    # Distinct `top` values, because uid_for keys on GEOMETRY rather than
    # text: two articles at one position on one page are one article by
    # design, and giving both the same box would dedupe them into one row.
    corpus.ingest([art(1, "A headline", "Body text that is long enough here.",
                       top=10),
                   art(2, "Another headline", "More body text, also long.",
                       top=80, category=catdata.UNKNOWN)],
                  job_id="j", paper="P", date="2026-08-30")
    cats = sorted(d["category"] for d in corpus.articles.find({}))
    assert cats == ["", catdata.UNKNOWN]


@needs_mongo
def test_tags_are_renormalised_on_the_way_in(corpus):
    """Ingest is also reachable from backfill, where values never met catdata."""
    corpus.ingest([art(1, "Headline", "Body text long enough to be indexed.",
                       tags=["OTP Fraud", " Rohini ", "OTP  Fraud"])],
                  job_id="j", paper="P", date="2026-08-30")
    assert corpus.articles.find_one({})["tags"] == ["otp fraud", "rohini"]


def _fill(c):
    rows = [
        art(1, "Two held for snatching", "Police arrested two men on Monday "
            "after a chain snatching near the market.", page=1,
            category="Crime", tags=["snatching", "rohini"], top=10),
        art(2, "Cyber cell busts fraud ring", "The cyber cell said the gang "
            "posed as bank staff to obtain one time passwords.", page=1,
            category="Cyber Crime", tags=["otp fraud"], top=60),
        art(3, "Assembly clears budget", "The assembly passed the annual "
            "budget after a debate on allocations for the year.", page=2,
            category="Politics", tags=["budget"], top=10),
        art(4, "Minister opens bridge", "The minister opened a new bridge "
            "connecting the two districts on Tuesday morning.", page=2,
            category="politics", tags=["bridge"], top=70),
    ]
    c.ingest(rows, job_id="j1", paper="Tribune", date="2026-08-30")
    c.ingest([art(1, "Later day story", "A story printed on the following "
                  "day, about a council meeting held on Wednesday.",
                  category="Civic")],
             job_id="j2", paper="Tribune", date="2026-08-31")


@needs_mongo
def test_dates_lists_days_with_counts(corpus):
    _fill(corpus)
    days = corpus.dates()
    assert [d["date"] for d in days] == ["2026-08-31", "2026-08-30"]
    assert days[1]["n"] == 4 and days[1]["labelled"] == 4


@needs_mongo
def test_facets_group_by_topic_for_one_day(corpus):
    _fill(corpus)
    f = corpus.facets(date="2026-08-30")
    assert f["total"] == 4
    counts = {c["category"]: c["n"] for c in f["categories"]}
    # "Cyber Crime" folded into cybercrime; "Politics" and "politics" are one
    assert counts == {"politics": 2, "crime": 1, "cybercrime": 1}
    assert [c["label"] for c in f["categories"] if c["category"] == "crime"] == ["Crime"]


@needs_mongo
def test_facets_report_unlabelled_separately(corpus):
    """A day nobody has classified must not look like a day with no crime."""
    corpus.ingest([art(1, "Unlabelled story", "Body text long enough to index.")],
                  job_id="j", paper="P", date="2026-09-01")
    f = corpus.facets(date="2026-09-01")
    assert f["total"] == 1
    assert f["unlabelled"] == 1
    assert f["categories"] == []


@needs_mongo
def test_facet_counts_match_what_browse_returns(corpus):
    """The count on a chip has to be the number of articles behind it."""
    _fill(corpus)
    for c in corpus.facets(date="2026-08-30")["categories"]:
        assert corpus.browse(date="2026-08-30",
                             category=c["category"])["total"] == c["n"]


@needs_mongo
def test_browse_returns_printed_order(corpus):
    _fill(corpus)
    r = corpus.browse(date="2026-08-30")
    assert [(h["page_number"], h["bbox"][1]) for h in r["results"]] == [
        (1, 10), (1, 60), (2, 10), (2, 70)]


@needs_mongo
def test_browse_accepts_the_label_as_displayed(corpus):
    """A reader arrives from a chip reading "Cybercrime", not "cybercrime"."""
    _fill(corpus)
    assert corpus.browse(date="2026-08-30", category="Cybercrime")["total"] == 1
    assert corpus.browse(date="2026-08-30", category="cybercrime")["total"] == 1


@needs_mongo
def test_browse_paginates(corpus):
    _fill(corpus)
    p1 = corpus.browse(date="2026-08-30", per_page=3, page=1)
    p2 = corpus.browse(date="2026-08-30", per_page=3, page=2)
    assert p1["pages"] == 2 and len(p1["results"]) == 3
    assert len(p2["results"]) == 1
    seen = [h["uid"] for h in p1["results"] + p2["results"]]
    assert len(set(seen)) == 4, "pages must not overlap or drop rows"


@needs_mongo
def test_browse_filters_by_tag(corpus):
    _fill(corpus)
    assert corpus.browse(date="2026-08-30", tag="otp fraud")["total"] == 1
    assert corpus.browse(date="2026-08-30", tag="OTP Fraud")["total"] == 1


@needs_mongo
def test_has_page_makes_ingest_resumable(corpus):
    _fill(corpus)
    assert corpus.has_page("Tribune", "2026-08-30", 1) is True
    assert corpus.has_page("Tribune", "2026-08-30", 9) is False
    assert corpus.has_page("Other", "2026-08-30", 1) is False


@needs_mongo
def test_stats_report_beats_and_topics_under_their_own_names(corpus):
    _fill(corpus)
    s = corpus.stats()
    assert {x["category"] for x in s["by_category"]} >= {"politics", "crime"}
    assert "by_beat" in s and isinstance(s["by_beat"], list)


@needs_mongo
def test_search_filters_by_topic_and_tag(corpus):
    _fill(corpus)
    assert corpus.search("police", record=False)["total"] >= 1
    hits = corpus.search("police", category="Crime", record=False)
    assert hits["total"] == 1
    assert hits["results"][0]["category"] == "crime"
    assert corpus.search("cyber", tag="otp fraud", record=False)["total"] == 1


@needs_mongo
def test_search_results_carry_both_fields(corpus):
    _fill(corpus)
    hit = corpus.search("police", record=False)["results"][0]
    for key in ("category", "primary_beat", "tags", "uid", "headline"):
        assert key in hit, "%s missing from a search result" % key


@needs_mongo
def test_browse_and_search_return_the_same_shape(corpus):
    """
    Both build on one base, so a field added for one cannot go missing from
    the other -- the drift that let `category` mean two things.
    """
    _fill(corpus)
    b = corpus.browse(date="2026-08-30")["results"][0]
    s = corpus.search("police", record=False)["results"][0]
    assert set(b) == set(s)
