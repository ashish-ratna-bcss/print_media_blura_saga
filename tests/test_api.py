"""
The HTTP surface the browse journey depends on.

These run against the real app with its real corpus connection, so they assert
on shape and contract rather than on counts -- a test that expects three crime
articles fails the moment somebody ingests a paper, which teaches everyone to
ignore it.

Nothing here writes to the corpus.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))

from conftest import needs_mongo  # noqa: E402


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import app  # noqa: E402
    return TestClient(app.app)


@needs_mongo
def test_dates_are_newest_first_with_counts(client):
    rows = client.get("/api/corpus/dates").json()
    assert isinstance(rows, list)
    for r in rows:
        assert set(r) == {"date", "n", "labelled"}
        assert r["labelled"] <= r["n"], "cannot label more than exist"
    assert rows == sorted(rows, key=lambda r: r["date"], reverse=True)


@needs_mongo
def test_facets_shape(client):
    rows = client.get("/api/corpus/dates").json()
    if not rows:
        pytest.skip("archive is empty")
    f = client.get("/api/facets?date=%s" % rows[0]["date"]).json()
    assert set(f) >= {"total", "unlabelled", "categories", "tags"}
    for c in f["categories"]:
        assert {"category", "label", "n", "children"} <= set(c)
        for k in c["children"]:
            assert {"subcategory", "label", "n"} <= set(k)
            assert k["n"] <= c["n"], "a child cannot hold more than its heading"
        # "" is reported through `unlabelled`, never as a topic in the picker
        assert c["category"]
    assert sum(c["n"] for c in f["categories"]) + f["unlabelled"] == f["total"]


@needs_mongo
def test_browse_requires_a_filter(client):
    """Unfiltered browse is "every article ever, page 1" -- not a question
    anybody asked, and slower every night the archive runs."""
    assert client.get("/api/browse").status_code == 400


@needs_mongo
def test_browse_pagination_is_consistent(client):
    rows = client.get("/api/corpus/dates").json()
    if not rows:
        pytest.skip("archive is empty")
    date = rows[0]["date"]
    r = client.get("/api/browse?date=%s&per_page=2" % date).json()
    assert r["page"] == 1 and r["per_page"] == 2
    assert len(r["results"]) <= 2
    assert r["pages"] == (r["total"] + 1) // 2 if r["total"] else True
    for h in r["results"]:
        assert {"uid", "headline", "category", "primary_beat", "tags",
                "page_number"} <= set(h)


@needs_mongo
def test_browse_per_page_is_capped(client):
    """An unbounded per_page is a way to ask for the whole archive at once."""
    rows = client.get("/api/corpus/dates").json()
    if not rows:
        pytest.skip("archive is empty")
    r = client.get("/api/browse?date=%s&per_page=9999" % rows[0]["date"]).json()
    assert r["per_page"] <= 200


@needs_mongo
def test_categories_listing_shape(client):
    body = client.get("/api/categories").json()
    assert "categories" in body
    for c in body["categories"]:
        assert {"id", "label", "n"} <= set(c)
        assert c["merged_into"] is None, "merged entries stay out of the picker"


@needs_mongo
def test_merge_rejects_bad_input(client):
    assert client.post("/api/categories/merge", json={}).status_code == 400
    assert client.post("/api/categories/merge",
                       json={"src": "x"}).status_code == 400
    assert client.post("/api/categories/merge",
                       json={"src": "nope-not-a-topic",
                             "dst": "crime"}).status_code == 404


@needs_mongo
def test_search_still_works_and_carries_both_fields(client):
    r = client.get("/api/search?q=police&limit=3")
    assert r.status_code == 200
    for h in r.json()["results"]:
        assert "category" in h and "primary_beat" in h


@needs_mongo
def test_corpus_stats_report_beats_and_topics(client):
    body = client.get("/api/corpus").json()
    assert "by_beat" in body and "by_category" in body


def test_health_is_always_answerable(client):
    """Must answer even with no model server -- it is how you find that out."""
    assert client.get("/api/health").status_code == 200
