"""
Job state in MongoDB behaves the way the SQLite it replaced did.

The cases that matter are the ones where the two stores differ. SQLite gave
every column a DEFAULT and computed seq as MAX(seq)+1 inside a lock; Mongo has
neither, so both are things this store has to provide rather than inherit.
"""

import pytest

from epaper import jobs as jobstore

from conftest import needs_mongo

pytestmark = needs_mongo


@pytest.fixture
def store(scratch_db):
    s = jobstore.JobStore(db=scratch_db)
    s.jobs.delete_many({})
    s.articles.delete_many({})
    s.db.job_counters.delete_many({})
    return s


def test_created_job_carries_every_default(store):
    """
    A field the caller did not set must still reach the frontend as 0 or "".

    SQLite declared these as DEFAULT clauses. Mongo would simply omit the key,
    and History renders `n_articles` into a table cell -- undefined there is a
    blank where a number belongs.
    """
    store.create("t-defaults", source="upload", paper="P")
    j = store.get("t-defaults")
    assert j["n_articles"] == 0
    assert j["coverage"] == 0.0
    assert j["kind"] == "page"
    assert j["error"] == ""
    assert j["source"] == "upload"
    assert "_id" not in j              # never serialisable by FastAPI


def test_set_updates_only_named_fields(store):
    store.create("j1", source="upload", paper="P", status="queued")
    store.set("j1", status="done", n_articles=7)
    j = store.get("j1")
    assert (j["status"], j["n_articles"]) == ("done", 7)
    assert j["paper"] == "P"           # untouched


def test_listing_is_newest_first(store):
    store.create("old", created="2026-01-01T00:00:00")
    store.create("new", created="2026-09-01T00:00:00")
    assert [j["id"] for j in store.listing()] == ["new", "old"]


def test_listing_respects_limit(store):
    for i in range(5):
        store.create("j%d" % i, created="2026-01-0%dT00:00:00" % (i + 1))
    assert len(store.listing(limit=2)) == 2


def test_rows_come_back_in_seq_order(store):
    store.create("j1")
    store.add_rows("j1", [{"headline": "one"}, {"headline": "two"},
                          {"headline": "three"}])
    rows = store.rows("j1")
    assert [r["headline"] for r in rows] == ["one", "two", "three"]
    assert [r["seq"] for r in rows] == [1, 2, 3]


def test_add_rows_appends_across_pages(store):
    """
    An edition writes one page at a time; the second must not restart at 1.

    This is what MAX(seq)+1 did in SQLite, and getting it wrong would collide
    on the unique index and fail a page for a reason unrelated to the page.
    """
    store.create("j1")
    store.add_rows("j1", [{"headline": "p1a"}, {"headline": "p1b"}], page_ref=1)
    store.add_rows("j1", [{"headline": "p2a"}], page_ref=2)
    rows = store.rows("j1")
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["page_ref"] for r in rows] == [1, 1, 2]


def test_reset_rows_replaces_and_renumbers(store):
    """A single-page re-run owns the job's rows; the old attempt must go."""
    store.create("j1")
    store.add_rows("j1", [{"headline": "first attempt"}])
    store.reset_rows("j1", [{"headline": "second"}, {"headline": "attempt"}])
    rows = store.rows("j1")
    assert [r["headline"] for r in rows] == ["second", "attempt"]
    assert [r["seq"] for r in rows] == [1, 2]


def test_rows_carry_article_defaults(store):
    store.create("j1")
    store.add_rows("j1", [{"headline": "h"}])
    r = store.rows("j1")[0]
    assert r["body"] == ""
    assert r["has_photo"] == 0
    assert r["type"] == "article"
    assert "_id" not in r


def test_delete_removes_the_job_and_its_rows(store):
    store.create("j1")
    store.add_rows("j1", [{"headline": "a"}, {"headline": "b"}])
    store.delete("j1")
    assert store.get("j1") is None
    assert store.rows("j1") == []


def test_delete_leaves_other_jobs_alone(store):
    store.create("j1")
    store.create("j2")
    store.add_rows("j1", [{"headline": "a"}])
    store.add_rows("j2", [{"headline": "b"}])
    store.delete("j1")
    assert store.get("j2") is not None
    assert [r["headline"] for r in store.rows("j2")] == ["b"]


def test_with_articles_is_oldest_first(store):
    """
    Backfill order decides which reading of a headline survives.

    An article dedupes on page geometry, so re-extracting one page writes the
    same document twice and the last write wins. Oldest first means the newest
    extraction is the one that lands.
    """
    store.create("old", created="2026-01-01T00:00:00", status="done")
    store.create("new", created="2026-09-01T00:00:00", status="done")
    store.add_rows("old", [{"headline": "SCtellsSIT"}])
    store.add_rows("new", [{"headline": "SC tells SIT"}])
    assert [j["id"] for j, _ in store.with_articles()] == ["old", "new"]


def test_with_articles_skips_jobs_that_produced_nothing(store):
    store.create("empty", created="2026-01-01T00:00:00", status="done")
    store.create("full", created="2026-01-02T00:00:00", status="done")
    store.add_rows("full", [{"headline": "a"}])
    assert [j["id"] for j, _ in store.with_articles()] == ["full"]


def test_get_returns_none_for_an_unknown_job(store):
    assert store.get("nope") is None
