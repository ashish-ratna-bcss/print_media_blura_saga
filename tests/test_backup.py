"""
Backup and restore.

Every test here defends against one failure mode, seen for real: a dump tool
that fails still leaves a file behind, because gzip will faithfully compress
an empty stream into a valid twenty-byte archive. Existence was taken as
success, so the log showed a tick and the archive held nothing -- invisible
until somebody needed a restore.

So the property under test is never "did it produce a file". It is:

  - a dump that could not read anything RAISES rather than writing a file
  - an archive that decompresses to nothing is REFUSED by verification
  - what comes back out equals what went in, document for document

The last one is the only test that really matters. A backup nobody has
restored is a hypothesis.
"""

from __future__ import annotations

import gzip
import importlib.util
import os

import pytest

from conftest import needs_mongo

from epaper import config


@pytest.fixture(scope="module")
def bk():
    """The script, loaded as a module -- it lives in scripts/, not a package."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location(
        "backup_script", os.path.join(root, "scripts", "backup.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded(scratch_db):
    """A scratch database holding a little of everything a corpus has."""
    from pymongo import MongoClient
    config.load_env()
    db = MongoClient(config.mongo_uri(), **config.mongo_kwargs())[scratch_db]
    db.articles.insert_many([
        {"uid": "a1", "headline": "Two held for snatching", "category": "crime",
         "subcategory": "snatching", "tags": ["rohini"], "date": "2026-08-30"},
        # Devanagari and a nested structure, because a JSON round trip that
        # mangles either is worse than no backup: it restores quietly wrong.
        {"uid": "a2", "headline": "बैंक धोखाधड़ी में तीन गिरफ्तार",
         "category": "crime", "subcategory": "cybercrime",
         "tags": ["otp fraud"], "bbox": [1, 2, 3, 4], "date": "2026-08-30"},
    ])
    db.categories.insert_one({"id": "crime", "label": "Crime", "n": 2})
    return scratch_db


@needs_mongo
def test_dump_refuses_a_database_with_nothing_in_it(bk, scratch_db, tmp_path,
                                                    monkeypatch):
    """
    The failure mode exactly: the dump reads nothing, gzip compresses the
    empty stream into a valid file, and existence is taken for success.
    """
    monkeypatch.setenv("EPAPER_MONGO_DB", scratch_db)
    dest = str(tmp_path / "empty.gz")
    with pytest.raises(RuntimeError) as ex:
        bk.dump_python(dest, progress=False)
    assert "no collections" in str(ex.value)
    assert not os.path.exists(dest), "nothing must be left that looks like a backup"


@needs_mongo
def test_verify_refuses_an_empty_archive(bk, tmp_path):
    p = str(tmp_path / "empty.gz")
    gzip.open(p, "wb").close()
    with pytest.raises(RuntimeError) as ex:
        bk.verify(p)
    assert "nothing" in str(ex.value)


@needs_mongo
def test_verify_refuses_an_archive_from_another_database(bk, tmp_path):
    """A dump of the wrong database is the failure a size check cannot see."""
    p = str(tmp_path / "other.gz")
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write('{"_meta":{"collections":["customers","invoices"]}}\n')
        fh.write('{"_c":"customers","_d":{"name":"x"}}\n' * 200)
    with pytest.raises(RuntimeError) as ex:
        bk.verify(p)
    assert "articles" in str(ex.value)


@needs_mongo
def test_verify_accepts_a_real_archive(bk, seeded, tmp_path, monkeypatch):
    monkeypatch.setenv("EPAPER_MONGO_DB", seeded)
    p = str(tmp_path / "ok.gz")
    bk.dump_python(p, progress=False)
    assert "articles" in bk.verify(p)["collections"]


@needs_mongo
def test_backup_restores_to_exactly_what_went_in(bk, seeded, tmp_path,
                                                 monkeypatch, scratch_db):
    """
    The only test that proves the backup is worth having.

    Round-trips through the archive into a second database and compares
    documents, not counts -- a restore that produces the right number of
    mangled documents is still a lost corpus.
    """
    from pymongo import MongoClient
    monkeypatch.setenv("EPAPER_MONGO_DB", seeded)
    p = str(tmp_path / "rt.gz")
    bk.dump_python(p, progress=False)

    target = seeded + "_restored"
    client = MongoClient(config.mongo_uri(), **config.mongo_kwargs())
    try:
        r = bk.restore(p, target_db=target, drop=False, dry=False)
        assert r["total"] == 3

        src, dst = client[seeded], client[target]
        for name in ("articles", "categories"):
            assert src[name].count_documents({}) == dst[name].count_documents({})
        for doc in src.articles.find({}):
            assert dst.articles.find_one({"_id": doc["_id"]}) == doc, (
                "a document changed in the round trip")
    finally:
        client.drop_database(target)


@needs_mongo
def test_restore_will_not_overwrite_a_live_database(bk, seeded, tmp_path,
                                                    monkeypatch):
    """Restoring over a populated corpus is nearly always a mistake."""
    monkeypatch.setenv("EPAPER_MONGO_DB", seeded)
    p = str(tmp_path / "rt.gz")
    bk.dump_python(p, progress=False)
    with pytest.raises(RuntimeError) as ex:
        bk.restore(p, target_db=seeded, drop=False, dry=False)
    assert "--drop" in str(ex.value)


@needs_mongo
def test_restore_refuses_an_archive_it_did_not_write(bk, tmp_path):
    p = str(tmp_path / "foreign.gz")
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        fh.write('{"_meta":{"format":"mongodump-archive"}}\n')
    with pytest.raises(RuntimeError) as ex:
        bk.restore(p, target_db="whatever", drop=False, dry=True)
    assert "mongorestore" in str(ex.value), "should name the right tool"


@needs_mongo
def test_dry_run_restore_writes_nothing(bk, seeded, tmp_path, monkeypatch):
    from pymongo import MongoClient
    monkeypatch.setenv("EPAPER_MONGO_DB", seeded)
    p = str(tmp_path / "rt.gz")
    bk.dump_python(p, progress=False)
    target = seeded + "_dry"
    client = MongoClient(config.mongo_uri(), **config.mongo_kwargs())
    try:
        r = bk.restore(p, target_db=target, drop=False, dry=True)
        assert r["dry_run"] and r["total"] == 3
        assert target not in client.list_database_names()
    finally:
        client.drop_database(target)


def test_preflight_only_demands_mongodump_for_that_engine(bk):
    """The python engine needs nothing beyond pymongo, and must say so."""
    problems = " ".join(bk.preflight("", need_s3=False, engine="python"))
    assert "mongodump" not in problems
