# Tests

```
pip install pytest
pytest                      # everything
pytest tests/test_catdata.py -v
pytest -k "merge or facet"
```

## What is covered, and what deliberately is not

| File | Defends |
| --- | --- |
| `test_catdata.py` | Label normalisation. Six spellings of one subject collapse to one entry; Hindi folds to English; a sentence is rejected rather than filed; the rules are idempotent and the alias table has no chains. |
| `test_classify.py` | That a label lands on the article it was written for. Renumbering, skipping, duplicate and out-of-range answers must produce an *unlabelled* article, never a *mislabelled* one. Also the retry path and failing safely when the model is dead. |
| `test_categories.py` | The topic registry: counting, merging, and that a merge moves the articles already filed *and* redirects future ones without building a chain. |
| `test_corpus_browse.py` | That `category` and `primary_beat` stay apart, and that the browse journey returns consistent numbers — a facet count equals the number of articles behind it, pages do not overlap, printed order is preserved. |
| `test_sources.py` | Scheduler behaviour: a repeated run does not double-collect, a failed run leaves the day due, failures accumulate visibly. |
| `test_api.py` | The HTTP contract for the browse journey. Asserts shape, not counts — a test expecting three crime articles fails the moment somebody ingests a paper, which teaches everyone to ignore it. |

**No model is ever called.** `fake_model` scripts what the classifier receives.
These tests are about batching, alignment, normalisation and storage — logic
that must hold whatever the model says, *including when it says something
wrong*. Whether the labels are any good is a different question, and it is
measured separately:

```
python scripts/classify_check.py --stability 20    # consistency, no human input
python scripts/classify_check.py --sample 60       # draw a set to grade by hand
python scripts/classify_check.py --score sample.json
```

**No test touches the live corpus.** Anything needing MongoDB gets a scratch
database named per test and dropped afterwards. Tests skip cleanly when no
MongoDB is reachable, so the suite still runs on a machine with neither a
database nor a GPU.

## Two things the suite found

Both were mistakes in the tests rather than the code, and both are worth
knowing before writing more:

- **`uid_for` keys on geometry, not text.** Two articles at the same position
  on the same page are one article by design — that is the fix for a duplicate
  row bug, and a test that gives two articles the same bbox will see one row.
- **Aliasing happens before merging.** A pair already in `catdata.ALIASES`
  never produces two entries, so it cannot be used to test `merge()`. Pick a
  pair the table does not know about.
