"""
Copy job history out of SQLite and into MongoDB.

    python scripts/migrate_jobs.py              show what would move
    python scripts/migrate_jobs.py --apply      move it

Idempotent: jobs already in MongoDB are left alone unless --force is given, so
running it twice does not duplicate history and a half-finished run can simply
be repeated.

The SQLite file is not touched. It stays on disk as the backup until you are
satisfied the History tab reads correctly, and deleting it is a separate,
manual decision.
"""

import argparse
import os
import sqlite3
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from epaper import jobs as jobstore                              # noqa: E402

SQLITE = os.path.join(ROOT, "web", "data", "jobs.db")


def rows_of(conn, table, where="", args=()):
    cur = conn.execute("SELECT * FROM %s %s" % (table, where), args)
    return [dict(r) for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to MongoDB")
    ap.add_argument("--force", action="store_true",
                    help="overwrite jobs already in MongoDB")
    ap.add_argument("--sqlite", default=SQLITE)
    args = ap.parse_args()

    if not os.path.exists(args.sqlite):
        print("no SQLite database at %s -- nothing to migrate" % args.sqlite)
        return 0

    store = jobstore.store()
    if not store:
        print("MongoDB unavailable: %s" % jobstore.last_error())
        return 2
    print("source  %s" % args.sqlite)
    print("target  %s / jobs + job_articles\n" % store.name)

    conn = sqlite3.connect(args.sqlite)
    conn.row_factory = sqlite3.Row
    jobs = rows_of(conn, "jobs")

    moved = skipped = rows_moved = 0
    for j in jobs:
        jid = j["id"]
        exists = store.jobs.find_one({"id": jid})
        if exists and not args.force:
            skipped += 1
            continue

        arts = rows_of(conn, "articles", "WHERE job_id=? ORDER BY seq", (jid,))
        moved += 1
        rows_moved += len(arts)
        if not args.apply:
            continue

        # Written with the seq SQLite already assigned rather than renumbered.
        # Result renders in seq order, and renumbering would reorder a page
        # whose rows were appended across several ingest passes.
        doc = dict(jobstore.JOB_DEFAULTS)
        doc.update({k: v for k, v in j.items() if v is not None})
        doc["id"] = jid
        store.jobs.replace_one({"id": jid}, doc, upsert=True)

        store.articles.delete_many({"job_id": jid})
        if arts:
            docs = []
            for a in arts:
                d = dict(jobstore.ARTICLE_DEFAULTS)
                d.update({k: v for k, v in a.items()
                          if k in jobstore.ARTICLE_DEFAULTS and v is not None})
                d["job_id"] = jid
                d["seq"] = a["seq"]
                docs.append(d)
            store.articles.insert_many(docs)
            # The counter appends continue from, so a job migrated mid-edition
            # does not restart numbering at 1 and collide on the unique index.
            store.db.job_counters.replace_one(
                {"_id": jid}, {"_id": jid, "seq": max(d["seq"] for d in docs)},
                upsert=True)

    print("%s" % ("APPLIED" if args.apply else "DRY RUN (nothing written)"))
    print("  jobs to move      %3d" % moved)
    print("  already present   %3d%s" % (skipped,
                                         "  (use --force to overwrite)"
                                         if skipped else ""))
    print("  article rows      %3d" % rows_moved)
    if args.apply:
        print("\nnow in mongodb: %d jobs, %d rows"
              % (store.jobs.count_documents({}),
                 store.articles.count_documents({})))
        print("\nThe SQLite file is untouched; delete it yourself once the")
        print("History tab looks right.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
