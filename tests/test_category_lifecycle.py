"""
A category has to survive the whole journey, not just get assigned.

Two bugs sat at opposite ends of that journey and both looked like "categories
don't work":

  The classifier's label was stripped on the way into the corpus, so Browse had
  nothing to group by while the job rows plainly held "politics" and "crime".

  A category created by hand was written, reported as created, and then never
  listed -- because the listing hid anything no article used yet, which is
  every hand-made category by definition.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))

from conftest import needs_mongo  # noqa: E402

from epaper import categories  # noqa: E402


@pytest.fixture
def store(scratch_db):
    s = categories.CategoryStore(db=scratch_db)
    s.cats.delete_many({})
    return s


# -- a hand-made category is usable immediately ----------------------------

@needs_mongo
def test_a_created_category_is_listed_before_anything_uses_it(store):
    """
    The whole point of creating one by hand is to have it ready in advance.

    It starts at n=0, so a listing that filters on n>=1 accepts the creation,
    reports success and then shows nothing -- which is what "manual category
    creation is not working" looked like from the outside.
    """
    store.create("Municipal corruption")
    ids = [c["id"] for c in store.listing()]
    assert "municipal corruption" in ids


@needs_mongo
def test_created_category_reports_zero_and_human_authorship(store):
    row = store.create("Municipal corruption")
    assert row["n"] == 0
    assert row["created_by"] == "human"
    assert row["label"] == "Municipal Corruption"


@needs_mongo
def test_the_count_floor_still_hides_unused_machine_labels(store):
    """
    The floor exists for a reason: one-off labels the classifier invented
    should not clutter the picker. Only hand-made entries are exempt.
    """
    store.cats.insert_one({"id": "a one off", "label": "A One Off", "n": 0,
                           "merged_into": None, "hidden": False,
                           "created_by": "classifier"})
    ids = [c["id"] for c in store.listing()]
    assert "a one off" not in ids


@needs_mongo
def test_hidden_and_merged_still_win_over_human_authorship(store):
    """Being hand-made must not override an explicit decision to hide it."""
    store.create("Hidden one")
    store.set_hidden("hidden one", True)
    assert "hidden one" not in [c["id"] for c in store.listing()]


@needs_mongo
def test_used_categories_still_sort_ahead_of_unused_ones(store):
    store.cats.insert_one({"id": "busy", "label": "Busy", "n": 12,
                           "merged_into": None, "hidden": False,
                           "created_by": "classifier"})
    store.create("Brand new")
    ids = [c["id"] for c in store.listing()]
    assert ids.index("busy") < ids.index("brand new")


# -- the classifier's label survives a backfill ----------------------------

def test_backfill_keeps_a_page_jobs_topic():
    """page and edition jobs write the classifier's subject; keep it."""
    import app  # noqa: E402
    rows = [{"headline": "h", "body": "b", "category": "politics"}]
    for kind in ("page", "edition", None):
        out = app.backfill_rows({"kind": kind}, rows)
        assert out[0]["category"] == "politics", kind


def test_backfill_drops_a_topic_jobs_beat_label():
    """
    A topic job writes "Delhi Police" there -- a force, not a subject.

    Letting it through files a police force as a topic and rebuilds the
    collision the primary_beat/category split exists to prevent.
    """
    import app  # noqa: E402
    out = app.backfill_rows(
        {"kind": "topic"}, [{"headline": "h", "category": "Delhi Police"}])
    assert "category" not in out[0]


def test_backfill_does_not_mutate_the_caller_s_rows():
    """
    The rows come from the job store and are reused by the caller's counters.

    Popping in place edited the stored dicts, so a second pass over the same
    list would see a different input than the first.
    """
    import app  # noqa: E402
    rows = [{"headline": "h", "category": "Delhi Police"}]
    app.backfill_rows({"kind": "topic"}, rows)
    assert rows[0]["category"] == "Delhi Police"
