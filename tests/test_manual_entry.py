"""
Setting topics by hand.

Automatic labelling is never going to be right about everything, so a person
has to be able to overrule it. The property that makes that worth doing is
that the correction sticks: a bulk relabel after a prompt change, or a
re-ingest of the same page, must leave a human answer alone. Without that the
next overnight run quietly undoes an afternoon of work, and nobody finds out
until they go looking for an article they had already fixed.
"""

from __future__ import annotations

import pytest

from conftest import needs_mongo

from epaper import catdata, categories
from epaper.corpus import Corpus


@pytest.fixture
def corpus(scratch_db, monkeypatch):
    c = Corpus(db=scratch_db, quiet=True)
    monkeypatch.setattr(categories, "_store",
                        categories.CategoryStore(db=scratch_db))
    return c


def _one(c, category=""):
    c.ingest([{"seq": 1, "type": "article", "headline": "Council clears tender",
               "body": "The council approved a waste collection tender at a "
                       "meeting held on Tuesday afternoon.",
               "page_ref": 1, "bbox": [0, 0, 100, 50], "category": category}],
             job_id="j", paper="P", date="2026-08-30")
    return c.articles.find_one({})["uid"]


@needs_mongo
def test_setting_a_topic_by_hand(corpus):
    uid = _one(corpus)
    out = corpus.set_topic(uid, category="Municipal Waste",
                           tags=["Landfill", " tender "])
    a = out["article"]
    assert a["category"] == "municipal waste"
    assert a["raw_category"] == "Municipal Waste"
    assert a["tags"] == ["landfill", "tender"]
    assert a["category_source"] == "human"


@needs_mongo
def test_a_hand_typed_topic_is_normalised_like_any_other(corpus):
    """Typed by a person or produced by a model, one subject is one entry."""
    uid = _one(corpus)
    corpus.set_topic(uid, category="Cyber Crime")
    assert corpus.articles.find_one({})["category"] == "cybercrime"


@needs_mongo
def test_a_hand_typed_topic_joins_the_picker(corpus):
    uid = _one(corpus)
    corpus.set_topic(uid, category="Municipal Waste")
    ids = [r["id"] for r in categories.store().listing()]
    assert "municipal waste" in ids


@needs_mongo
def test_a_correction_survives_a_bulk_relabel(corpus, fake_model):
    """The property the whole feature depends on."""
    uid = _one(corpus)
    corpus.set_topic(uid, category="Municipal Waste")
    fake_model([{"n": 1, "topic": "Politics", "tags": []}])
    r = corpus.reclassify(only_unlabelled=False)
    assert r["scanned"] == 0, "a human-set topic must not be re-asked"
    assert corpus.articles.find_one({})["category"] == "municipal waste"


@needs_mongo
def test_model_labels_are_still_relabelled(corpus, fake_model):
    """The skip must be narrow: only human answers, not every labelled row."""
    uid = _one(corpus, category="Crime")
    assert corpus.articles.find_one({"uid": uid})["category"] == "crime"
    fake_model([{"n": 1, "topic": "Politics", "tags": []}])
    r = corpus.reclassify(only_unlabelled=False)
    assert r["scanned"] == 1
    assert corpus.articles.find_one({})["category"] == "politics"


@needs_mongo
def test_clearing_a_topic_is_allowed_and_remembered(corpus):
    """
    Deciding an article has no useful topic is a decision too, and it has to
    stick -- otherwise the next run refills it with the answer just rejected.
    """
    uid = _one(corpus, category="Crime")
    corpus.set_topic(uid, category="")
    d = corpus.articles.find_one({})
    assert d["category"] == ""
    assert d["category_source"] == "human"


@needs_mongo
def test_tags_can_be_set_without_touching_the_topic(corpus):
    uid = _one(corpus, category="Crime")
    corpus.set_topic(uid, tags=["tender"])
    d = corpus.articles.find_one({})
    assert d["tags"] == ["tender"]
    assert d["category"] == "crime"
    assert d.get("category_source") != "human", "only the tags were set"


@needs_mongo
def test_unknown_article_is_reported(corpus):
    with pytest.raises(KeyError):
        corpus.set_topic("no-such-uid", category="crime")


@needs_mongo
def test_creating_a_topic_before_anything_uses_it(scratch_db):
    """
    Starts at zero articles, not one. Counting it as a sighting would put a
    topic at the top of the review queue that no article carries.
    """
    s = categories.CategoryStore(db=scratch_db)
    row = s.create("Municipal Waste")
    assert row["id"] == "municipal waste"
    assert row["label"] == "Municipal Waste"
    assert row["n"] == 0
    assert row["created_by"] == "human"
    assert "municipal waste" in [r["id"] for r in s.listing(min_count=0)]


@needs_mongo
def test_creating_an_existing_topic_does_not_reset_its_count(scratch_db):
    s = categories.CategoryStore(db=scratch_db)
    s.observe("Crime")
    s.observe("Crime")
    s.create("crime")
    assert s.get("crime")["n"] == 2


@needs_mongo
def test_a_sentence_is_refused_as_a_topic(scratch_db):
    s = categories.CategoryStore(db=scratch_db)
    with pytest.raises(ValueError):
        s.create("this is a whole sentence describing the article at length")


@needs_mongo
def test_a_created_topic_can_be_a_merge_target(scratch_db):
    """The reason creation exists at all: a merge needs somewhere to go."""
    s = categories.CategoryStore(db=scratch_db)
    s.observe("roadworks")
    s.create("Civic Works")
    r = s.merge("roadworks", "civic works", articles=s.db.articles)
    assert r["into"] == "civic works"
    assert s.resolve("roadworks") == "civic works"


@needs_mongo
def test_unusable_labels_never_reach_storage(corpus):
    """set_topic files through catdata, so nothing unusable is stored."""
    uid = _one(corpus)
    corpus.set_topic(uid, category="   ")
    assert corpus.articles.find_one({})["category"] == ""
    assert catdata.normalise("a sentence that runs on far too long to be one") == ""
