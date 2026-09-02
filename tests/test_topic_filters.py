"""
The HTTP contract for the Topic Search refinements.

Shape and validation rather than counts: these run against the real corpus,
and a test asserting "three results" fails the moment somebody ingests a
paper, which teaches everyone to ignore the suite.

Nothing here fetches a page. The archive scope exists precisely so a search
can be answered without one, and the fetch scope is only exercised as far as
its argument validation -- running it would cost minutes of model time and
reach the network.
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


# -- /api/search ------------------------------------------------------------

@needs_mongo
def test_search_accepts_the_refinements(client):
    r = client.get("/api/search", params={
        "q": "police", "exclude": "traffic", "proximity": "w10",
        "weekdays": "mon,tue", "limit": 5})
    assert r.status_code == 200
    assert "results" in r.json()


@needs_mongo
def test_exclusion_never_returns_the_excluded_word(client):
    """
    The property that matters, stated as a property.

    MongoDB's $text treats a negated term as a scoring hint rather than a
    filter, so an implementation built on it returns the very articles it was
    asked to suppress -- and does it quietly, which is why this is asserted
    over every result rather than spot-checked.
    """
    rows = client.get("/api/search", params={
        "q": "police", "exclude": "court", "limit": 50}).json()["results"]
    for r in rows:
        blob = "%s %s" % (r.get("headline") or "", r.get("snippet") or "")
        assert "court" not in blob.lower(), r.get("headline")


@needs_mongo
def test_all_seven_weekdays_matches_no_filter_at_all(client):
    """Selecting every day and selecting none both mean "do not filter"."""
    base = client.get("/api/search", params={"q": "police", "limit": 100}).json()
    every = client.get("/api/search", params={
        "q": "police", "limit": 100,
        "weekdays": "mon,tue,wed,thu,fri,sat,sun"}).json()
    assert base["total"] == every["total"]


@needs_mongo
def test_narrowing_never_grows_the_result(client):
    """Each refinement is a filter; none of them can add anything."""
    base = client.get("/api/search", params={"q": "police", "limit": 200}).json()
    for extra in ({"exclude": "court"}, {"proximity": "exact"},
                  {"weekdays": "mon"}, {"timeframe": "today"}):
        got = client.get("/api/search",
                         params=dict({"q": "police", "limit": 200}, **extra)).json()
        assert got["total"] <= base["total"], extra


@needs_mongo
def test_timeframe_resolves_server_side(client):
    """
    Sent as a word, not a pair of dates.

    Resolving in the browser would let a request made at 23:59 and served at
    00:01 mean a different week to the one the reader was looking at.
    """
    r = client.get("/api/search", params={"q": "police", "timeframe": "month"})
    assert r.status_code == 200


# -- /api/jobs/topic --------------------------------------------------------

@needs_mongo
def test_archive_scope_needs_only_a_topic(client):
    """
    No host, no title, no date.

    Requiring them would be asking the reader to name the page they want
    before they are allowed to search for it.
    """
    r = client.post("/api/jobs/topic", json={
        "topic": "police", "scope": "archive", "timeframe": "all"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "archive"


@needs_mongo
def test_archive_search_finishes_without_polling(client):
    """
    One indexed read, so the job is done by the time the call returns.

    A queued job the caller then has to poll would add latency to hide
    latency that does not exist.
    """
    jid = client.post("/api/jobs/topic", json={
        "topic": "police", "scope": "archive", "timeframe": "all"}).json()["job_id"]
    job = client.get("/api/jobs/%s" % jid).json()["job"]
    assert job["status"] == "done", job.get("error")
    assert "no pages fetched" in (job.get("note") or "")


@needs_mongo
def test_fetch_scope_still_demands_a_page_to_read(client):
    r = client.post("/api/jobs/topic", json={"topic": "police"})
    assert r.status_code == 400
    assert "host" in r.json().get("detail", "")


@needs_mongo
@pytest.mark.parametrize("bad", ["within 3 words", "w7", "close"])
def test_unknown_proximity_is_rejected(client, bad):
    """
    Rejected, not silently ignored.

    A misspelled setting quietly falling back to "anywhere" returns a wider
    result than was asked for and looks like a working search.
    """
    r = client.post("/api/jobs/topic", json={
        "topic": "police", "scope": "archive", "proximity": bad})
    assert r.status_code == 400


@needs_mongo
@pytest.mark.parametrize("bad", ["fortnight", "last-year", "2026"])
def test_unknown_timeframe_is_rejected(client, bad):
    r = client.post("/api/jobs/topic", json={
        "topic": "police", "scope": "archive", "timeframe": bad})
    assert r.status_code == 400


@needs_mongo
def test_archive_job_records_the_span_it_searched(client):
    """The note is the only place the resolved dates are visible afterwards."""
    jid = client.post("/api/jobs/topic", json={
        "topic": "police", "scope": "archive", "timeframe": "week"}).json()["job_id"]
    note = client.get("/api/jobs/%s" % jid).json()["job"]["note"]
    assert " to " in note and "ms" in note


# -- timeframe on the browse endpoints -------------------------------------

@needs_mongo
def test_browse_accepts_a_timeframe_instead_of_a_date(client):
    r = client.get("/api/browse", params={"timeframe": "week", "per_page": 5})
    assert r.status_code == 200
    assert "results" in r.json()


@needs_mongo
def test_facets_accepts_a_timeframe(client):
    r = client.get("/api/facets", params={"timeframe": "month"})
    assert r.status_code == 200
    assert "categories" in r.json()


@needs_mongo
def test_browse_still_needs_some_narrowing(client):
    """
    Without a filter this is "every article ever extracted, page 1" -- not a
    question anybody asked, and slower every night the archive runs.
    """
    assert client.get("/api/browse").status_code == 400


@needs_mongo
def test_an_explicit_date_beats_a_timeframe(client):
    """
    Both given, the specific one wins -- otherwise picking a day from the
    archive list would be silently overridden by a preset left selected.
    """
    rows = client.get("/api/browse", params={
        "date": "2026-09-02", "timeframe": "month", "per_page": 100}).json()
    assert all(r["date"] == "2026-09-02" for r in rows["results"])


@needs_mongo
def test_every_browse_result_can_address_its_clipping(client):
    """
    The card leads with the cut-out, so the fields that address it have to be
    on every hit -- not just the ones the search path happened to return.
    """
    rows = client.get("/api/browse", params={
        "timeframe": "month", "per_page": 20}).json()["results"]
    if not rows:
        pytest.skip("archive is empty")
    for r in rows:
        assert "crop" in r and "job_id" in r
        assert "date" in r and "page_number" in r
        assert "subcategory" in r
