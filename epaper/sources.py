"""
Which papers to collect, and when each was last collected.

Until now the answer lived in whoever typed the request: a job named its host,
title and date, and nothing recorded that the paper was meant to be fetched
again tomorrow. An unattended nightly run needs that written down, which is
what this is -- one row per title/edition we intend to keep, plus the state
the scheduler needs to avoid doing the same work twice.

    sources   host, title id, display name, language, whether it is enabled,
              how many pages to take, and the outcome of the last attempt.

`last_date` is the important field and the reason this is not a config file.
The scheduler asks "what have I not collected for today" rather than "what
time is it", so a run that is late, interrupted or repeated does the right
thing: it picks up what is missing and skips what is done. A cron entry that
fires twice does not double-collect, and one that is missed does not silently
skip a day -- the gap is still visible in `last_date` the next night.

Failures are recorded rather than raised. An e-paper that has not published
yet at 2am is the normal case, not an error, and it must not stop the other
titles. `last_error` and `fail_count` are what make a title that has been
quietly failing for a week visible without reading a log.
"""

from __future__ import annotations

import datetime as _dt

from pymongo import ASCENDING, MongoClient

from . import config


def source_id(host: str, title_id) -> str:
    """One row per title within a host. Editions of one paper are separate."""
    return "%s:%s" % ((host or "").strip().lower(), str(title_id or "").strip())


class SourceStore:
    def __init__(self, uri: str | None = None, db: str | None = None):
        self.uri = config.mongo_uri(uri)
        self.name = config.mongo_db(db)
        self.client = MongoClient(self.uri, **config.mongo_kwargs())
        self.db = self.client[self.name]
        self.sources = self.db.sources
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.sources.create_index([("id", ASCENDING)], unique=True)
        self.sources.create_index([("enabled", ASCENDING)])

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    # -- editing -----------------------------------------------------------

    def add(self, *, host: str, title_id, paper: str = "", edition: str = "",
            lang: str = "", max_pages: int = 0, enabled: bool = True) -> dict:
        """
        Register a title, or update the settings of one already registered.

        Collection state -- last_date, last_error, fail_count -- is set only on
        insert. Editing a paper's page limit must not make the scheduler think
        it has never been collected and re-fetch an archive's worth of issues.
        """
        if not host or not str(title_id or "").strip():
            raise ValueError("a source needs a host and a title_id")
        sid = source_id(host, title_id)
        now = _dt.datetime.now()
        self.sources.update_one(
            {"id": sid},
            {"$set": {"id": sid, "host": host, "title_id": str(title_id),
                      "paper": paper or host, "edition": edition,
                      "lang": lang, "max_pages": int(max_pages or 0),
                      "enabled": bool(enabled), "updated_at": now},
             "$setOnInsert": {"last_date": "", "last_run": None,
                              "last_error": "", "last_error_date": "",
                      "fail_count": 0,
                              "created_at": now}},
            upsert=True)
        return self.get(sid)

    def get(self, sid: str):
        return self.sources.find_one({"id": sid}, {"_id": 0})

    def remove(self, sid: str) -> dict:
        r = self.sources.delete_one({"id": sid})
        if not r.deleted_count:
            raise KeyError(sid)
        return {"deleted": sid}

    def set_enabled(self, sid: str, on: bool) -> dict:
        if not self.sources.find_one({"id": sid}):
            raise KeyError(sid)
        self.sources.update_one({"id": sid},
                                {"$set": {"enabled": bool(on),
                                          "updated_at": _dt.datetime.now()}})
        return self.get(sid)

    def set_max_pages(self, sid: str, max_pages: int) -> dict:
        """
        How many pages of this title to take each night. 0 is the whole paper.

        Separate from add() so changing a limit cannot touch last_date. add()
        protects that with $setOnInsert, but a caller reaching for the whole
        upsert to change one number is one typo away from re-collecting an
        archive, and this makes that impossible rather than merely unlikely.
        """
        if not self.sources.find_one({"id": sid}):
            raise KeyError(sid)
        self.sources.update_one(
            {"id": sid}, {"$set": {"max_pages": max(0, int(max_pages or 0)),
                                   "updated_at": _dt.datetime.now()}})
        return self.get(sid)

    # -- reading -----------------------------------------------------------

    def listing(self, *, enabled_only: bool = False) -> list:
        flt = {"enabled": True} if enabled_only else {}
        return list(self.sources.find(flt, {"_id": 0}).sort("paper", ASCENDING))

    def due(self, date: str) -> list:
        """
        Enabled titles not yet collected for `date`.

        The scheduler's only question. Comparing against a stored date rather
        than a timestamp is what makes a repeated run idempotent and a missed
        run recoverable -- both reduce to "is this date already done".
        """
        return list(self.sources.find(
            {"enabled": True, "last_date": {"$ne": date}}, {"_id": 0}
        ).sort("paper", ASCENDING))

    # -- recording ---------------------------------------------------------

    def mark_done(self, sid: str, date: str, *, pages: int = 0,
                  articles: int = 0) -> dict:
        """
        A successful collection. Clears the failure state.

        All of it, including which day failed -- a stale error date would keep
        colouring the paper red on the day it just succeeded.
        """
        self.sources.update_one(
            {"id": sid},
            {"$set": {"last_date": date, "last_run": _dt.datetime.now(),
                      "last_error": "", "last_error_date": "", "fail_count": 0,
                      "last_pages": int(pages), "last_articles": int(articles)}})
        return self.get(sid)

    def mark_failed(self, sid: str, error: str, date: str = "") -> dict:
        """
        A failed attempt. `last_date` is deliberately NOT advanced, so the
        next run tries again rather than treating the day as covered.

        `date` records WHICH day failed. Without it the only signal is
        "has ever failed", and the Daily Reader showed a paper as failed
        today because it had failed yesterday -- before anything had been
        attempted today at all.
        """
        up = {"last_run": _dt.datetime.now(), "last_error": str(error)[:300]}
        if date:
            up["last_error_date"] = date
        self.sources.update_one({"id": sid},
                                {"$set": up, "$inc": {"fail_count": 1}})
        return self.get(sid)


# ------------------------------------------------------------------ helper

_store = None
_error = ""


def store(**kw):
    """Process-wide SourceStore, or None with the reason in `last_error()`."""
    global _store, _error
    if _store is None:
        try:
            s = SourceStore(**kw)
            if not s.ping():
                raise RuntimeError("mongodb not reachable")
            _store, _error = s, ""
        except Exception as ex:
            _error = str(ex)[:200]
            return None
    return _store


def last_error() -> str:
    return _error
