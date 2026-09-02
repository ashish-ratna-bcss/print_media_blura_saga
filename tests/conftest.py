"""
Shared fixtures.

Two rules the whole suite depends on.

NEVER THE LIVE CORPUS. Every test that needs MongoDB gets its own scratch
database, named per test and dropped afterwards. The connection URI comes from
`.env` like everything else, but the database name never does -- an earlier
regression harness wrote its fixtures into the working corpus, and the point
of that story is that the mistake is invisible until somebody searches.

NEVER THE MODEL. `fake_model` replaces vlm.text_json with a scripted answer.
The tests here are about batching, alignment, normalisation and storage --
logic that must hold whatever the model says, including when it says something
wrong. Label quality is a different question, measured by
scripts/classify_check.py against a real model.
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import config  # noqa: E402


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient
        config.load_env()
        MongoClient(config.mongo_uri(), serverSelectionTimeoutMS=1500,
                    **{k: v for k, v in config.mongo_kwargs().items()
                       if k != "serverSelectionTimeoutMS"}
                    ).admin.command("ping")
        return True
    except Exception:
        return False


HAVE_MONGO = _mongo_available()
needs_mongo = pytest.mark.skipif(
    not HAVE_MONGO, reason="no MongoDB reachable; storage tests skipped")


@pytest.fixture
def scratch_db():
    """A uniquely named database, dropped when the test ends."""
    from pymongo import MongoClient
    config.load_env()
    name = "epaper_test_%s" % uuid.uuid4().hex[:10]
    client = MongoClient(config.mongo_uri(), **config.mongo_kwargs())
    try:
        yield name
    finally:
        client.drop_database(name)
        client.close()


@pytest.fixture
def fake_model(monkeypatch):
    """
    Script what the model returns.

        fake_model([{"n": 1, "topic": "Crime", "tags": ["theft"]}])

    Pass a list of items for one answer, or a list of lists to give a
    different answer to each successive call -- which is how the retry paths
    are tested, since they depend on the second call differing from the first.
    """
    from epaper import vlm

    def script(answers, error=None):
        calls = {"n": 0, "prompts": []}
        batched = answers and isinstance(answers[0], list)

        def fake(prompt, max_tokens=0, timeout=0):
            calls["prompts"].append(prompt)
            i = calls["n"]
            calls["n"] += 1
            if error and i < (error if isinstance(error, int) else 1):
                raise RuntimeError("model exploded")
            if not batched:
                return {"items": answers}
            return {"items": answers[min(i, len(answers) - 1)]}

        monkeypatch.setattr(vlm, "text_json", fake)
        return calls

    return script
