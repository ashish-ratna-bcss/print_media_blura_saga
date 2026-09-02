"""
Topic labelling: alignment, retries, and failing safely.

The single property everything here defends is that a label lands on the
article it was written for. A model that renumbers, skips or invents an entry
must produce an UNLABELLED article, never a MISLABELLED one -- an unlabelled
article is a visible gap, while a mislabelled one is wrong and looks fine.

No model is called. What a real model says is measured separately by
scripts/classify_check.py; what matters here is that the code is correct
whatever it says.
"""

from __future__ import annotations

from epaper import catdata, classify


def arts(n, prefix="Article"):
    """n articles with enough text to be classifiable."""
    return [{"headline": "%s %d headline here" % (prefix, i + 1),
             "body": "Body text for article %d, long enough to be worth "
                     "labelling and to pass the minimum length check." % (i + 1)}
            for i in range(n)]


def test_labels_land_on_the_right_articles(fake_model):
    fake_model([{"n": 1, "topic": "Crime", "tags": ["theft"]},
                {"n": 2, "topic": "Politics", "tags": ["budget"]},
                {"n": 3, "topic": "Sports", "tags": ["cricket"]}])
    out = classify.classify(arts(3))
    assert [o["category"] for o in out] == ["crime", "politics", "sports"]
    assert out[0]["tags"] == ["theft"]


def test_raw_label_is_kept_beside_the_filed_one(fake_model):
    """
    The merge tooling reasons about what the model actually wrote. Without it,
    a bad alias cannot be told apart from a bad classification.
    """
    fake_model([{"n": 1, "category": "crime", "subcategory": "Cyber Crime",
                 "tags": []}])
    out = classify.classify(arts(1))
    assert out[0]["category"] == "crime"
    assert out[0]["subcategory"] == "cybercrime"
    assert out[0]["raw_category"] == "crime"


def test_an_off_list_heading_becomes_a_subcategory(fake_model):
    """
    The model is told to choose a heading from the list and sometimes will not.
    Its answer is still a correct reading of the article, so it is demoted to
    the second level and placed by its seed parent rather than discarded --
    losing the classification over a formatting mistake would be worse.
    """
    fake_model([{"n": 1, "category": "Cyber Crime", "tags": []}])
    out = classify.classify(arts(1))
    assert out[0]["category"] == "crime", "placed by catdata.SEED_PARENTS"
    assert out[0]["subcategory"] == "cybercrime"


def test_a_subcategory_never_repeats_its_parent(fake_model):
    fake_model([{"n": 1, "category": "crime", "subcategory": "Crime",
                 "tags": []}])
    assert classify.classify(arts(1))[0]["subcategory"] == ""


def test_out_of_range_numbers_are_dropped(fake_model):
    """
    A model numbering an article that was not in the batch must not have its
    answer filed against a real one.
    """
    # Batched form, so the retries answer nothing. A single canned answer
    # replayed to every call would be answering about whichever article the
    # retry renumbered as 1, which is not what a real model does.
    fake_model([
        [{"n": 1, "topic": "Crime", "tags": []},
         {"n": 99, "topic": "Sports", "tags": []},
         {"n": 0, "topic": "Business", "tags": []},
         {"n": "x", "topic": "Health", "tags": []}],
        [], [],
    ])
    out = classify.classify(arts(2))
    assert out[0]["category"] == "crime"
    # article 2 was never answered for, so it is unlabelled -- not "sports"
    assert out[1]["category"] == catdata.UNKNOWN
    assert out[1]["classify_error"]


def test_duplicate_numbers_keep_the_first(fake_model):
    fake_model([{"n": 1, "topic": "Crime", "tags": []},
                {"n": 1, "topic": "Sports", "tags": []}])
    out = classify.classify(arts(1))
    assert out[0]["category"] == "crime"


def test_skipped_articles_are_retried_then_marked(fake_model):
    """
    The measured failure: a small model answers for some of a batch and stops.
    The gap is detected and re-asked; whatever is still missing is recorded as
    unlabelled rather than inheriting a neighbour's topic.
    """
    calls = fake_model([
        [{"n": 1, "topic": "Crime", "tags": []}],       # first ask: 1 of 3
        [{"n": 1, "topic": "Politics", "tags": []}],    # retry: 1 of the 2 left
        [],                                             # per-article: nothing
    ])
    out = classify.classify(arts(3))
    assert out[0]["category"] == "crime"
    assert out[1]["category"] == "politics"
    assert out[2]["category"] == catdata.UNKNOWN
    assert calls["n"] > 1, "the gap should have been re-asked"


def test_retry_recovers_everything_when_the_model_settles(fake_model):
    fake_model([
        [{"n": 1, "topic": "Crime", "tags": []}],
        [{"n": 1, "topic": "Politics", "tags": []},
         {"n": 2, "topic": "Sports", "tags": []}],
    ])
    out = classify.classify(arts(3))
    assert [o["category"] for o in out] == ["crime", "politics", "sports"]
    assert not any(o.get("classify_error") for o in out)


def test_retries_are_bounded(fake_model):
    """An unbounded retry against a model that has stopped answering is an
    overnight job that never finishes."""
    calls = fake_model([[]])
    classify.classify(arts(3))
    assert calls["n"] <= 1 + 1 + 3, "expected one ask, one retry, then per-article"


def test_a_dead_model_leaves_articles_unlabelled_not_lost(fake_model):
    fake_model([], error=99)
    out = classify.classify(arts(4))
    assert len(out) == 4
    assert all(o["category"] == catdata.UNKNOWN for o in out)
    assert all("failed" in o["classify_error"] for o in out)


def test_short_articles_are_never_sent(fake_model):
    """
    A batch of blanks returns confident nonsense, and it would be filed.
    """
    calls = fake_model([{"n": 1, "topic": "Crime", "tags": []}])
    items = [{"headline": "", "body": ""},
             {"headline": "x", "body": ""}] + arts(1)
    out = classify.classify(items)
    assert out[0]["category"] == catdata.UNKNOWN
    assert "too little text" in out[0]["classify_error"]
    assert out[2]["category"] == "crime"
    for p in calls["prompts"]:
        assert "1. " in p and "2. " not in p, "only the usable article is sent"


def test_output_is_always_aligned_to_input(fake_model):
    """Callers index positionally; a short answer must not shift the list."""
    for n in (0, 1, 7, 10, 11, 25):
        fake_model([{"n": 1, "topic": "Crime", "tags": []}])
        assert len(classify.classify(arts(n))) == n


def test_batching_splits_large_inputs(fake_model):
    calls = fake_model([{"n": 1, "topic": "Crime", "tags": []}])
    classify.classify(arts(classify.BATCH * 2))
    assert calls["n"] >= 2, "more than one batch should have been sent"


def test_progress_reports_to_the_end(fake_model):
    fake_model([{"n": i + 1, "topic": "Crime", "tags": []}
                for i in range(classify.BATCH)])
    seen = []
    n = classify.BATCH * 2
    classify.classify(arts(n), progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1] == (n, n)


def test_summarise_counts_failures(fake_model):
    fake_model([[{"n": 1, "topic": "Crime", "tags": []}], [], []])
    s = classify.summarise(classify.classify(arts(2)))
    assert s["labelled"] == 1
    assert s["failed"] == 1
    assert s["topics"]["crime"] == 1
