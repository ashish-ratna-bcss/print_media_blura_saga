"""
The nightly source list.

Everything here is about a scheduler running with nobody watching: a run that
fires twice must not double-collect, a run that is missed must not silently
skip a day, and one paper failing must not hide behind a green exit code.
"""

from __future__ import annotations

import pytest

from conftest import needs_mongo

from epaper import sources


@pytest.fixture
def store(scratch_db):
    return sources.SourceStore(db=scratch_db)


@needs_mongo
def test_add_is_idempotent(store):
    for _ in range(3):
        store.add(host="h.com", title_id="1", paper="Paper")
    assert len(store.listing()) == 1


@needs_mongo
def test_editing_settings_does_not_reset_collection_history(store):
    """
    Otherwise changing a page limit makes the scheduler think the title has
    never been collected and re-fetch an archive's worth of issues.
    """
    store.add(host="h.com", title_id="1", paper="Paper")
    store.mark_done("h.com:1", "2026-08-30", pages=12, articles=90)
    store.add(host="h.com", title_id="1", paper="Paper", max_pages=4)
    row = store.get("h.com:1")
    assert row["last_date"] == "2026-08-30"
    assert row["max_pages"] == 4


@needs_mongo
def test_due_is_idempotent_across_repeated_runs(store):
    store.add(host="h.com", title_id="1", paper="Paper")
    assert [s["id"] for s in store.due("2026-08-30")] == ["h.com:1"]
    store.mark_done("h.com:1", "2026-08-30")
    assert store.due("2026-08-30") == []
    # a different day is due again
    assert [s["id"] for s in store.due("2026-08-31")] == ["h.com:1"]


@needs_mongo
def test_a_failure_leaves_the_day_due(store):
    """
    The missed-run property: a failed attempt must not advance last_date, or
    the day is treated as covered and never retried.
    """
    store.add(host="h.com", title_id="1", paper="Paper")
    store.mark_failed("h.com:1", "not published yet")
    row = store.get("h.com:1")
    assert row["last_date"] == ""
    assert row["fail_count"] == 1
    assert "not published" in row["last_error"]
    assert [s["id"] for s in store.due("2026-08-30")] == ["h.com:1"]


@needs_mongo
def test_repeated_failures_accumulate_then_clear_on_success(store):
    """A title failing quietly every night has to be visible without a log."""
    store.add(host="h.com", title_id="1", paper="Paper")
    for _ in range(3):
        store.mark_failed("h.com:1", "timeout")
    assert store.get("h.com:1")["fail_count"] == 3
    store.mark_done("h.com:1", "2026-08-30")
    row = store.get("h.com:1")
    assert row["fail_count"] == 0 and row["last_error"] == ""


@needs_mongo
def test_disabled_titles_are_not_due(store):
    store.add(host="h.com", title_id="1", paper="Paper")
    store.set_enabled("h.com:1", False)
    assert store.due("2026-08-30") == []
    assert len(store.listing()) == 1, "disabled, not forgotten"
    assert store.listing(enabled_only=True) == []


@needs_mongo
def test_editions_of_one_host_are_separate_rows(store):
    store.add(host="h.com", title_id="delhi", paper="Paper Delhi")
    store.add(host="h.com", title_id="pune", paper="Paper Pune")
    assert len(store.listing()) == 2


@needs_mongo
def test_rejects_incomplete_registration(store):
    with pytest.raises(ValueError):
        store.add(host="", title_id="1")
    with pytest.raises(ValueError):
        store.add(host="h.com", title_id="")


@needs_mongo
def test_remove_reports_a_missing_row(store):
    with pytest.raises(KeyError):
        store.remove("nope:1")
