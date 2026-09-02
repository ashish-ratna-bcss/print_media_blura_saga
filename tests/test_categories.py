"""
The topic registry: counting, merging, and not losing decisions.

Merging is the only operation here that changes articles already filed, so
most of these tests are about it doing exactly what it says and nothing more.
"""

from __future__ import annotations

import pytest

from conftest import needs_mongo

from epaper import categories


@pytest.fixture
def store(scratch_db):
    return categories.CategoryStore(db=scratch_db)


@needs_mongo
def test_variants_collapse_to_one_entry(store):
    for label in ["Cybercrime", "Cyber Crime", "cyber-crime", "Online Fraud"]:
        store.observe(label, label)
    rows = store.listing()
    assert [r["id"] for r in rows] == ["cybercrime"]
    assert rows[0]["n"] == 4


@needs_mongo
def test_observe_returns_the_entry_to_file_under(store):
    assert store.observe("Cyber Crime") == "cybercrime"


@needs_mongo
def test_display_label_and_sample_are_kept(store):
    store.observe("Cyber Crime", "Cyber Crime")
    row = store.get("cybercrime")
    assert row["label"] == "Cybercrime"
    assert row["sample_raw"] == "Cyber Crime"


@needs_mongo
def test_merge_moves_articles_and_redirects_future_ones(store):
    # Deliberately a pair catdata does NOT alias. An aliased pair never
    # produces two entries in the first place -- normalisation is the earlier
    # and cheaper path, and merge exists only for what it cannot know.
    arts = store.db.articles
    for _ in range(3):
        arts.insert_one({"category": store.observe("Roadworks")})
    store.observe("Infrastructure")

    r = store.merge("roadworks", "infrastructure", articles=arts)
    assert r["articles_moved"] == 3
    assert arts.count_documents({"category": "roadworks"}) == 0
    assert arts.count_documents({"category": "infrastructure"}) == 3
    # and the next sighting of the old label goes to the new home
    assert store.observe("Roadworks") == "infrastructure"
    assert store.resolve("roadworks") == "infrastructure"


@needs_mongo
def test_merged_entry_leaves_the_picker_but_not_the_database(store):
    store.observe("Roadworks")
    store.observe("Infrastructure")
    store.merge("roadworks", "infrastructure", articles=store.db.articles)
    assert "roadworks" not in [r["id"] for r in store.listing()]
    # kept, so the decision survives and can be undone
    assert store.get("roadworks") is not None


@needs_mongo
def test_merge_into_a_new_entry_creates_it(store):
    store.observe("Roadworks")
    store.merge("roadworks", "law reports", articles=store.db.articles)
    assert store.get("law reports") is not None


@needs_mongo
def test_merge_never_builds_a_chain(store):
    """
    A -> B -> C would make resolve() wrong at one hop. Pointers to the old
    target are re-pointed instead, so every chain is flattened as it forms.
    """
    for label in ["a", "b", "c"]:
        store.observe(label)
    store.merge("a", "b", articles=store.db.articles)
    store.merge("b", "c", articles=store.db.articles)
    assert store.resolve("a") == "c"
    assert store.resolve("b") == "c"


@needs_mongo
def test_merge_rejects_nonsense(store):
    store.observe("crime")
    with pytest.raises(ValueError):
        store.merge("crime", "crime", articles=store.db.articles)
    with pytest.raises(KeyError):
        store.merge("nope", "crime", articles=store.db.articles)


@needs_mongo
def test_unmerge_restores_the_entry_and_says_what_it_cannot_undo(store):
    store.observe("Roadworks")
    store.observe("Infrastructure")
    store.merge("roadworks", "infrastructure", articles=store.db.articles)
    r = store.unmerge("roadworks")
    assert store.resolve("roadworks") == "roadworks"
    assert "not moved back" in r["note"]


@needs_mongo
def test_hidden_entries_stay_stored(store):
    store.observe("other")
    store.set_hidden("other", True)
    assert "other" not in [r["id"] for r in store.listing()]
    assert "other" in [r["id"] for r in store.listing(include_hidden=True)]


@needs_mongo
def test_rebuild_makes_counts_agree_with_the_articles(store):
    arts = store.db.articles
    store.observe("crime")
    store.observe("crime")
    arts.insert_one({"category": "crime"})          # only one really exists
    arts.insert_one({"category": "politics"})       # never observed
    r = store.rebuild(arts)
    assert r["articles"] == 2
    assert store.get("crime")["n"] == 1
    assert store.get("politics")["n"] == 1


@needs_mongo
def test_file_as_degrades_without_the_database(monkeypatch):
    """
    A database that is down costs the counts and the merge redirects. It must
    never cost the label, and never the article.
    """
    monkeypatch.setattr(categories, "_store", None)
    monkeypatch.setattr(categories, "store", lambda **kw: None)
    assert categories.file_as("Cyber Crime") == "cybercrime"
    assert categories.file_as("") == "unclassified"
