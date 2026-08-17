#!/usr/bin/env python3
"""
Move a database to a new name.

    python scripts/db_rename.py --from epaper --to epaper_corpus
    python scripts/db_rename.py --from epaper --to epaper_corpus --drop-source

MongoDB has no renameDatabase, so this copies every collection document by
document, rebuilds the indexes, verifies the counts match, and only then --
and only when asked -- drops the source. The default leaves the old database
exactly where it was, because a rename that turns out to be wrong should cost
a second command, not a restore.

Ordinary upserts, keyed on the document's own _id, so running it twice is
harmless and an interrupted run resumes rather than duplicating.

Indexes are recreated from the source's own definitions rather than assumed,
which matters for the text index on `articles`: it carries
default_language="none" for the bilingual corpus, and a copy that quietly lost
that setting would apply the English stemmer to Devanagari.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient                                    # noqa: E402
from pymongo.errors import OperationFailure                        # noqa: E402

from epaper import config                                          # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--to", dest="dst", required=True)
    ap.add_argument("--drop-source", action="store_true",
                    help="delete the source database once the copy is verified")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    args = ap.parse_args()

    if args.src == args.dst:
        sys.exit("--from and --to are the same database")
    for n in (args.src, args.dst):
        if n in config.RESERVED_DBS:
            sys.exit("%r is one of MongoDB's own databases" % n)

    config.load_env()
    uri = config.mongo_uri()
    print("host      %s" % config.hosts(uri))
    print("copy      %s  ->  %s\n" % (args.src, args.dst))

    client = MongoClient(uri, **config.mongo_kwargs())
    client.admin.command("ping")
    src, dst = client[args.src], client[args.dst]

    names = sorted(src.list_collection_names())
    if not names:
        sys.exit("%r has no collections -- nothing to do" % args.src)

    plan = [(n, src[n].count_documents({})) for n in names]
    for n, c in plan:
        existing = dst[n].count_documents({})
        print("   %-16s %6d document(s)%s"
              % (n, c, "   (target already holds %d)" % existing if existing else ""))
    if not args.yes:
        print("\ncontinue? [y/N] ", end="", flush=True)
        if (sys.stdin.readline().strip().lower() or "n") not in ("y", "yes"):
            sys.exit("cancelled")

    print()
    for name, _ in plan:
        copy_collection(src[name], dst[name])

    print("\nverify")
    ok = True
    for name, count in plan:
        got = dst[name].count_documents({})
        good = got >= count
        ok = ok and good
        print("   %-5s %-16s %d -> %d" % ("ok" if good else "FAIL", name, count, got))

    if not ok:
        sys.exit("\ncounts do not match. Source left untouched.")

    if args.drop_source:
        client.drop_database(args.src)
        print("\ndropped %r" % args.src)
    else:
        print("\n%r still exists. Once %r looks right:" % (args.src, args.dst))
        print("   python scripts/db_rename.py --from %s --to %s --drop-source --yes"
              % (args.src, args.dst))

    print("\nSet the name so everything points at it:")
    print("   EPAPER_MONGO_DB=%s      (in .env)" % args.dst)
    print("Then restart the web app -- a running process keeps the database it "
          "started with.")


def copy_collection(src, dst):
    n = 0
    for doc in src.find({}):
        dst.update_one({"_id": doc["_id"]}, {"$set": doc}, upsert=True)
        n += 1
    print("   %-16s %6d copied" % (src.name, n))
    copy_indexes(src, dst)


def copy_indexes(src, dst):
    """
    Recreate the source's indexes, preserving their options.

    Text indexes need care: the key is stored as _fts/_ftsx with the real
    fields in `weights`, so replaying the key verbatim creates something
    invalid. Reconstruct it from weights instead, and carry
    default_language across.
    """
    for name, spec in src.index_information().items():
        if name == "_id_":
            continue
        keys = list(spec.get("key", []))
        opts = {k: v for k, v in spec.items()
                if k in ("unique", "sparse", "expireAfterSeconds",
                         "partialFilterExpression", "default_language")}
        if any(k[0] in ("_fts", "_ftsx") for k in keys):
            fields = list(spec.get("weights") or {})
            if not fields:
                continue
            keys = [(f, "text") for f in fields]
            opts.setdefault("default_language", "none")
        try:
            dst.create_index(keys, name=name, **opts)
        except OperationFailure as ex:
            print("      index %r skipped: %s" % (name, str(ex)[:100]))


if __name__ == "__main__":
    sys.exit(main())
