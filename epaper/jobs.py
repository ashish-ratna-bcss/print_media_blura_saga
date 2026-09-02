"""
Job state in MongoDB, so the project keeps one database instead of two.

Extraction runs used to be recorded in a SQLite file next to the app while the
articles they produced went to MongoDB. The split was defensible -- job state
answers "how did this run go", the corpus answers "what has this paper ever
printed" -- but it meant repointing EPAPER_MONGO_DB gave you a fresh corpus and
a History tab still listing every run against the old one, which is confusing
in exactly the way one store is not.

What it costs: MongoDB is no longer optional. It used to be possible to run an
extraction with mongod down and backfill the corpus later, because the job and
its articles were safe in SQLite. Now a database that is down stops the app.
That is the trade for a single store, and it is why `available()` exists --
callers that want to fail politely can ask before they start.

The shape deliberately mirrors the SQLite schema it replaces, field names
included, so the API responses and the frontend reading them do not change.
Two collections:

    jobs           one document per run, keyed by the same 12-hex id
    job_articles   the rows Result renders, keyed (job_id, seq)

`_id` is stripped on the way out. It is Mongo's business, the frontend has
never seen it, and FastAPI cannot serialise an ObjectId anyway.
"""

import datetime as _dt
import threading

from . import config

try:
    from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
    from pymongo.errors import PyMongoError
except ImportError:                                  # pragma: no cover
    MongoClient = None
    PyMongoError = Exception


# Defaults the SQLite schema declared as DEFAULT clauses. Mongo has no such
# thing, and a missing key would reach the frontend as undefined where it used
# to arrive as 0 -- so every job document is created with all of them.
JOB_DEFAULTS = {
    "source": "", "paper": "", "edition": "", "date": "", "page_no": 0,
    "status": "queued", "stage": "queued", "error": "",
    "n_articles": 0, "n_ads": 0, "coverage": 0.0,
    "width": 0, "height": 0, "with_text": 0, "elapsed": 0.0,
    "kind": "page", "topic": "", "searched": 0, "screened": 0,
    "mode": "", "beat": "", "note": "", "prefiltered": 0,
    "n_pages": 0, "pages_done": 0, "pages_failed": 0, "pages_skipped": 0,
}

ARTICLE_DEFAULTS = {
    "type": "article", "headline": "", "body": "", "byline": "",
    "dateline": "", "category": "", "bbox": "", "crop": "",
    "n_elements": 0, "has_photo": 0, "topic_score": 0.0, "topic_why": "",
    "topic_summary": "", "page_ref": 0, "keywords": "",
}

_store = None
_error = ""
_lock = threading.Lock()


def _clean(doc):
    """A document as the API should see it: no _id, defaults filled in."""
    if not doc:
        return None
    out = dict(doc)
    out.pop("_id", None)
    return out


class JobStore:
    def __init__(self, uri: str | None = None, db: str | None = None,
                 client=None):
        # Same connection route as Corpus, not a second one. epaper.config
        # owns the URI, the database name and the timeouts, so moving to Atlas
        # stays a one-file change and the job store cannot end up pointed at a
        # different database than the corpus it belongs to.
        self.uri = config.mongo_uri(uri)
        self.name = config.mongo_db(db)
        self.client = client or MongoClient(self.uri, **config.mongo_kwargs())
        self.db = self.client[self.name]
        self.jobs = self.db.jobs
        self.articles = self.db.job_articles
        self.ensure_indexes()

    def ensure_indexes(self):
        self.jobs.create_index([("id", ASCENDING)], unique=True)
        # History's only query: newest first, capped.
        self.jobs.create_index([("created", DESCENDING)])
        self.jobs.create_index([("status", ASCENDING)])
        # Result reads a job's rows in printed order; delete_job removes them
        # by job. Unique on (job_id, seq) so a retried page cannot silently
        # write the same row twice.
        self.articles.create_index([("job_id", ASCENDING), ("seq", ASCENDING)],
                                   unique=True)

    def ping(self) -> bool:
        self.client.admin.command("ping")
        return True

    # -- jobs ------------------------------------------------------------

    def create(self, jid: str, **fields) -> str:
        doc = dict(JOB_DEFAULTS)
        doc.update({k: v for k, v in fields.items() if v is not None})
        doc["id"] = jid
        doc.setdefault("created",
                       _dt.datetime.now().isoformat(timespec="seconds"))
        self.jobs.insert_one(doc)
        return jid

    def set(self, jid: str, **fields):
        """The UPDATE ... SET that set_job used to issue."""
        if not fields:
            return
        self.jobs.update_one({"id": jid}, {"$set": fields})

    def get(self, jid: str):
        return _clean(self.jobs.find_one({"id": jid}))

    def listing(self, limit: int = 60, kinds=None) -> list:
        """
        Recent jobs, newest first, optionally restricted to certain kinds.

        The restriction is part of the QUERY, not a filter over the result.
        Fetching the newest 60 and then keeping the page jobs among them shows
        nothing at all after a night that ran 60 editions -- the reader's own
        extractions are still there, just past the cut.
        """
        flt = {}
        if kinds:
            want = sorted({k for k in kinds if k})
            if want:
                # A job written before `kind` existed is a page job; match
                # those too rather than hiding history behind a schema change.
                flt["kind"] = ({"$in": want + [None]} if "page" in want
                               else {"$in": want})
        cur = self.jobs.find(flt).sort("created", DESCENDING).limit(int(limit))
        return [_clean(d) for d in cur]

    def delete(self, jid: str):
        self.jobs.delete_one({"id": jid})
        self.articles.delete_many({"job_id": jid})

    # -- a job's article rows --------------------------------------------

    def rows(self, jid: str) -> list:
        cur = self.articles.find({"job_id": jid}).sort("seq", ASCENDING)
        return [_clean(d) for d in cur]

    def clear_rows(self, jid: str):
        self.articles.delete_many({"job_id": jid})

    def next_seq(self, jid: str) -> int:
        """
        The seq an appended row should take.

        SQLite computed this as MAX(seq)+1 inside the same lock that did the
        insert. An edition writes one page at a time and pages can overlap, so
        the read and the write have to be one operation or two pages racing
        both get the same seq -- which the unique index would then reject,
        failing a page for a reason that has nothing to do with the page.
        A counter incremented in place is that one operation.
        """
        doc = self.db.job_counters.find_one_and_update(
            {"_id": jid}, {"$inc": {"seq": 1}},
            upsert=True, return_document=ReturnDocument.AFTER)
        return int(doc["seq"])

    def add_rows(self, jid: str, rows: list, page_ref=None) -> int:
        """
        Append rows for a job, numbering them as SQLite's MAX(seq)+1 did.

        Returns how many were written.
        """
        if not rows:
            return 0
        docs = []
        for a in rows:
            d = dict(ARTICLE_DEFAULTS)
            d.update({k: v for k, v in a.items() if k in ARTICLE_DEFAULTS})
            d["job_id"] = jid
            d["seq"] = self.next_seq(jid)
            if page_ref is not None:
                d["page_ref"] = page_ref
            docs.append(d)
        self.articles.insert_many(docs)
        return len(docs)

    def reset_rows(self, jid: str, rows: list, page_ref=None) -> int:
        """Replace a job's rows -- what a single-page run does."""
        self.clear_rows(jid)
        self.db.job_counters.delete_one({"_id": jid})
        return self.add_rows(jid, rows, page_ref=page_ref)

    # -- backfill --------------------------------------------------------

    def with_articles(self, oldest_first: bool = True) -> list:
        """
        Every job that produced rows, with them, for corpus backfill.

        Oldest first by default and explicitly: an article dedupes on a uid
        keyed to page geometry, so several extractions of one page write the
        same document and the last write wins. Ordering decides which reading
        of a headline survives, and the newest should.
        """
        order = ASCENDING if oldest_first else DESCENDING
        out = []
        for j in self.jobs.find({}).sort("created", order):
            rows = self.rows(j["id"])
            if rows:
                out.append((_clean(j), rows))
        return out


def store():
    """The shared JobStore, or None with the reason in `last_error()`."""
    global _store, _error
    if _store is not None:
        return _store
    with _lock:
        if _store is not None:
            return _store
        if MongoClient is None:
            _error = "pymongo is not installed"
            return None
        try:
            s = JobStore()
            s.ping()
            _store, _error = s, ""
        except Exception as ex:
            _error = str(ex)[:200]
            return None
    return _store


def last_error() -> str:
    return _error


def available() -> bool:
    return store() is not None
