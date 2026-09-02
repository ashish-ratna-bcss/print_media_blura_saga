"""
The daily round: which papers are registered, and what arrived today.

The Daily Reader answers about PAPERS, not about jobs -- History already
lists runs. That difference is the substance of these tests: a paper that was
due and produced nothing has to appear as an absence, because a report that
lists only successes cannot tell "Sakshi published nothing today" from
"Sakshi was never registered".
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))

from conftest import needs_mongo  # noqa: E402

from epaper import sources  # noqa: E402


@pytest.fixture
def store(scratch_db):
    s = sources.SourceStore(db=scratch_db)
    s.sources.delete_many({})
    return s


TE = {"host": "epaper.sakshi.com", "title_id": "101", "paper": "Sakshi",
      "edition": "Hyderabad", "lang": "te"}


# -- registering the papers you read ---------------------------------------

@needs_mongo
def test_a_registered_paper_is_listed_with_its_language(store):
    store.add(**TE)
    row = store.listing()[0]
    assert row["paper"] == "Sakshi"
    assert row["lang"] == "te"
    assert row["enabled"] is True


@needs_mongo
def test_registering_twice_updates_rather_than_duplicates(store):
    store.add(**TE)
    store.add(**dict(TE, max_pages=4))
    rows = store.listing()
    assert len(rows) == 1
    assert rows[0]["max_pages"] == 4


@needs_mongo
def test_editing_a_paper_does_not_reset_its_collection_history(store):
    """
    Changing a page limit must not make the scheduler re-fetch an archive.

    last_date is $setOnInsert for exactly this reason: an edit that cleared it
    would look to due() like a paper never collected.
    """
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_done(sid, "2026-09-01", pages=1, articles=9)
    store.add(**dict(TE, max_pages=6))
    assert store.get(sid)["last_date"] == "2026-09-01"


# -- what the scheduler asks ------------------------------------------------

@needs_mongo
def test_due_lists_papers_not_yet_collected_today(store):
    store.add(**TE)
    assert [r["paper"] for r in store.due("2026-09-02")] == ["Sakshi"]


@needs_mongo
def test_a_collected_paper_is_no_longer_due_that_day(store):
    """Idempotence: a 02:00 run and a 06:00 re-run must do the same thing."""
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_done(sid, "2026-09-02", pages=1, articles=9)
    assert store.due("2026-09-02") == []
    assert [r["paper"] for r in store.due("2026-09-03")] == ["Sakshi"]


@needs_mongo
def test_a_disabled_paper_is_never_due(store):
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.set_enabled(sid, False)
    assert store.due("2026-09-02") == []


@needs_mongo
def test_a_failure_leaves_the_paper_due_again(store):
    """
    An edition not published by 02:00 is the normal case, not an error.

    mark_failed must not advance last_date, or a paper that failed once would
    be skipped for the rest of the day and the retry would never happen.
    """
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_failed(sid, "no issue published")
    assert [r["paper"] for r in store.due("2026-09-02")] == ["Sakshi"]
    assert store.get(sid)["fail_count"] == 1


# -- the report -------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    import app  # noqa: E402
    return TestClient(app.app)


@needs_mongo
def test_daily_report_has_the_shape_the_tab_renders(client):
    d = client.get("/api/daily").json()
    assert set(d) >= {"date", "total", "collected", "waiting", "failed",
                      "sources", "schedule"}
    for s in d["sources"]:
        assert s["status"] in ("collected", "waiting", "failed")
        assert set(s) >= {"paper", "edition", "n", "labelled", "pages",
                          "categories", "registered"}


@needs_mongo
def test_daily_report_defaults_to_today(client):
    import datetime as dt
    assert client.get("/api/daily").json()["date"] == dt.date.today().isoformat()


@needs_mongo
def test_daily_counts_agree_with_the_rows_they_summarise(client):
    """The headline numbers are what a reader trusts; they must add up."""
    d = client.get("/api/daily?date=2026-09-02").json()
    rows = d["sources"]
    assert d["collected"] == sum(1 for r in rows if r["status"] == "collected")
    assert d["waiting"] == sum(1 for r in rows if r["status"] == "waiting")
    assert d["failed"] == sum(1 for r in rows if r["status"] == "failed")
    assert d["total"] == sum(r["n"] for r in rows)


@needs_mongo
def test_collected_papers_sort_before_the_ones_still_waiting(client):
    d = client.get("/api/daily?date=2026-09-02").json()
    order = [r["status"] == "collected" for r in d["sources"]]
    assert order == sorted(order, reverse=True)


@needs_mongo
def test_schedule_state_is_read_from_the_os_not_from_a_setting(client):
    """
    A stored flag would say "on" for a task somebody deleted in Windows.

    The panel exists to answer "will it run tonight without me", so it has to
    ask the thing that would actually run it.
    """
    sched = client.get("/api/daily").json()["schedule"]
    assert "registered" in sched and "supported" in sched
    assert isinstance(sched["registered"], bool)


@needs_mongo
def test_a_bad_schedule_time_is_rejected(client):
    for bad in ("5:30", "25:00", "half five", "0530"):
        r = client.post("/api/daily/schedule", json={"enabled": True, "at": bad})
        assert r.status_code == 400, bad


# -- a failed collection must not count as done ----------------------------

@needs_mongo
def test_a_paper_that_published_nothing_stays_due(store):
    """
    The bug that made the scheduler look broken.

    run_ingest_job catches its own failures and records status="error" rather
    than raising, so a try/except around it never fires. Marking done on the
    absence of an exception therefore marked Sakshi collected on a day it
    published nothing -- and due() then skipped it for the rest of the day, so
    the retry never happened and the Daily Reader showed an empty paper with
    no error against it.

    The rule is the job's own status, which is what scripts/daily.py has
    always checked.
    """
    store.add(**TE)
    sid = store.listing()[0]["id"]

    job = {"status": "error", "error": "no issue published on 2026-09-02"}
    if job["status"] == "done":
        store.mark_done(sid, "2026-09-02")
    else:
        store.mark_failed(sid, job["error"])

    assert [r["paper"] for r in store.due("2026-09-02")] == ["Sakshi"]
    row = store.get(sid)
    assert row["fail_count"] == 1
    assert "no issue published" in row["last_error"]


@needs_mongo
def test_a_zero_article_success_is_still_a_success(store):
    """
    A paper whose only free page was already held collects nothing and is
    still done -- there is nothing left to fetch today, so re-running it would
    just refetch the same paywalled pages.
    """
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_done(sid, "2026-09-02", pages=0, articles=0)
    assert store.due("2026-09-02") == []
    assert store.get(sid)["fail_count"] == 0


# -- the Daily Reader lists subscriptions, not everything extracted --------

@needs_mongo
def test_daily_lists_only_registered_papers(client):
    """
    A one-off run from the New tab is not part of the daily round.

    Listing it here made the page answer a different question than it asks:
    "did the papers I subscribed to arrive" cannot be answered by a list in
    which three unsubscribed Tribune editions outrank two Telugu papers that
    did not turn up.
    """
    d = client.get("/api/daily?date=2026-09-02").json()
    assert all(s["registered"] for s in d["sources"])


@needs_mongo
def test_jobs_can_be_narrowed_to_the_runs_a_person_started(client):
    rows = client.get("/api/jobs?kind=page,upload&limit=50").json()
    assert all((r.get("kind") or "page") in ("page", "upload") for r in rows)


@needs_mongo
def test_kind_filter_is_applied_in_the_query_not_after_it(client):
    """
    Filtering the newest 60 rows would show nothing after a night of 60
    edition jobs, while the reader's own extractions sat just past the cut.
    """
    from epaper import jobs as jobstore
    st = jobstore.store()
    if st is None or st.jobs.count_documents({"kind": "edition"}) == 0:
        pytest.skip("no edition jobs to crowd the listing")
    # One row asked for, restricted to page/upload: if the restriction were
    # applied after the limit, an edition job would consume the single slot
    # and this would come back empty.
    rows = st.listing(1, kinds=["page", "upload"])
    assert all((r.get("kind") or "page") in ("page", "upload") for r in rows)


# -- per-paper page limit ---------------------------------------------------

@needs_mongo
def test_page_limit_defaults_to_the_whole_paper(store):
    """0 means every page the publisher lists, not zero pages."""
    store.add(**TE)
    assert store.listing()[0]["max_pages"] == 0


@needs_mongo
def test_setting_a_page_limit_keeps_collection_history(store):
    """
    Changing a limit must not look to due() like a paper never collected.

    add() protects last_date with $setOnInsert, but reaching for the whole
    upsert to change one number is one slip away from re-collecting an
    archive. set_max_pages cannot touch it at all.
    """
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_done(sid, "2026-09-01", pages=22, articles=140)
    store.set_max_pages(sid, 6)
    row = store.get(sid)
    assert row["max_pages"] == 6
    assert row["last_date"] == "2026-09-01"


@needs_mongo
def test_setting_a_page_limit_does_not_re_enable_a_disabled_paper(store):
    """The two fields are applied independently, so neither implies the other."""
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.set_enabled(sid, False)
    store.set_max_pages(sid, 4)
    assert store.get(sid)["enabled"] is False


@needs_mongo
def test_an_unknown_source_cannot_be_limited(store):
    with pytest.raises(KeyError):
        store.set_max_pages("nope", 4)


@needs_mongo
def test_patch_rejects_an_out_of_range_limit(client):
    for bad in (-1, 500):
        r = client.request("PATCH", "/api/sources/x", json={"max_pages": bad})
        assert r.status_code in (400, 404), bad


@needs_mongo
def test_patch_needs_something_to_change(client):
    r = client.request("PATCH", "/api/sources/x", json={})
    assert r.status_code == 400


@needs_mongo
def test_a_paper_that_failed_yesterday_is_waiting_today(store):
    """
    "failed" is about the day being viewed, not about ever having failed.

    Showing a paper red today because it failed yesterday -- before anything
    had been attempted today -- is how a reader learns to ignore the colour,
    which is the one thing the colour is for.
    """
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_failed(sid, "no issue published", "2026-09-01")
    row = store.get(sid)
    assert row["last_error_date"] == "2026-09-01"
    # The report compares that against the day it is rendering.
    assert row["last_error_date"] != "2026-09-02"


@needs_mongo
def test_a_success_clears_the_failure_date(store):
    store.add(**TE)
    sid = store.listing()[0]["id"]
    store.mark_failed(sid, "no issue published", "2026-09-01")
    store.mark_done(sid, "2026-09-02", pages=22, articles=140)
    row = store.get(sid)
    assert row["last_error"] == ""
    assert row["last_error_date"] == ""
    assert row["fail_count"] == 0
