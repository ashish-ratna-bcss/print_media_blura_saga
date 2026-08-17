#!/usr/bin/env python3
"""
Is the database reachable, and is it the one we meant?

    python scripts/db_check.py              # local or Atlas, whatever .env says
    python scripts/db_check.py --test       # the test database instead
    python scripts/db_check.py --write      # also prove writes work, then undo

Read-only unless you pass --write. Prints no passwords.

Most of this file is failure diagnosis, because Atlas's failures are
misleading. An IP that is not on the access list does not produce a
permission error -- it produces a connection timeout identical to the cluster
being down, and people spend an hour on the password. So each error is mapped
back to the thing that actually causes it.

`connectionStatus` is the part worth reading after rotating a credential: it
reports the roles the server thinks you have. A user that still says
`atlasAdmin` or `readWriteAnyDatabase` has not been scoped down, whatever the
Atlas UI appeared to save.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pymongo import MongoClient                                    # noqa: E402
from pymongo import errors as mongo_errors                         # noqa: E402

from epaper import config                                          # noqa: E402

OK, BAD, WARN = "  ok ", " FAIL", " warn"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true",
                    help="check EPAPER_MONGO_TEST_DB instead of the corpus")
    ap.add_argument("--db", help="check this database name")
    ap.add_argument("--write", action="store_true",
                    help="insert and delete one document, to prove write access")
    args = ap.parse_args()

    config.load_env()
    name = args.db or (os.environ.get("EPAPER_MONGO_TEST_DB", "epaper_corpus_test")
                       if args.test else config.mongo_db())
    uri = config.mongo_uri()

    print("configuration")
    print("   env file   %s" % (config.ENV_FILE if os.path.exists(config.ENV_FILE)
                                else "(none -- using defaults)"))
    print("   uri        %s" % config.redact(uri))
    print("   hosts      %s" % config.hosts(uri))
    print("   database   %s" % name)
    print("   kind       %s" % ("Atlas" if config.is_atlas(uri) else "self-hosted"))
    print("   set by     EPAPER_MONGO from %s, EPAPER_MONGO_DB from %s"
          % (config.source("EPAPER_MONGO"), config.source("EPAPER_MONGO_DB")))
    if config.source("EPAPER_MONGO") == "environment" and os.path.exists(config.ENV_FILE):
        print("   note       EPAPER_MONGO is a real environment variable, so it "
              "overrides .env. Editing .env will not change anything until you "
              "clear it:  [Environment]::SetEnvironmentVariable("
              "'EPAPER_MONGO',$null,'User')")

    print("\nconnection")
    t0 = dt.datetime.now()
    try:
        client = MongoClient(uri, **config.mongo_kwargs())
        client.admin.command("ping")
    except Exception as ex:
        print("%s ping" % BAD)
        explain(ex, uri)
        return 1
    ms = (dt.datetime.now() - t0).total_seconds() * 1000
    print("%s ping                    %.0f ms" % (OK, ms))
    if config.is_atlas(uri) and ms > 800:
        print("%s round trip is slow. Search reads a lot of small documents, and "
              "every one pays this. A cluster in the same region as this "
              "machine is the fix." % WARN)

    try:
        info = client.server_info()
        print("%s server                  MongoDB %s" % (OK, info.get("version")))
    except Exception as ex:
        print("%s server_info: %s" % (WARN, str(ex)[:100]))

    who(client, name)
    contents(client, name)
    if args.write:
        write_probe(client, name)

    print("\nready.")
    return 0


def who(client, name):
    """Which user, holding which roles. The check that matters after rotation."""
    try:
        st = client.admin.command("connectionStatus")["authInfo"]
    except Exception:
        return
    users = st.get("authenticatedUsers") or []
    roles = st.get("authenticatedUserRoles") or []
    if not users:
        print("%s auth                    none (unauthenticated connection)" % WARN)
        return
    print("%s auth                    %s"
          % (OK, ", ".join(u.get("user", "?") for u in users)))
    for r in roles:
        role, on = r.get("role", "?"), r.get("db", "?")
        broad = (role in ("atlasAdmin", "readWriteAnyDatabase", "root",
                          "dbAdminAnyDatabase", "readAnyDatabase")
                 or on == "admin")
        flag = WARN if broad else OK
        print("%s role                    %s on %s%s"
              % (flag, role, on,
                 "   <- reaches databases beyond %s" % name if broad else ""))
    if any(r.get("role") in ("atlasAdmin", "readWriteAnyDatabase", "root")
           for r in roles):
        print("%s this credential can read and write every database on the "
              "cluster. For this app it only needs readWrite on %r."
              % (WARN, name))


def contents(client, name):
    """What is actually in there. Also catches 'connected, but to nothing'."""
    print("\ndatabase %r" % name)
    db = client[name]
    try:
        cols = sorted(db.list_collection_names())
    except mongo_errors.OperationFailure as ex:
        print("%s cannot list collections: %s" % (BAD, str(ex)[:120]))
        print("       the user lacks read access to this database")
        return
    if not cols:
        print("%s empty -- no collections yet. Expected on a fresh database; "
              "the first extraction creates them." % WARN)
        return
    for c in cols:
        print("%s %-16s %8d documents" % (OK, c, db[c].count_documents({})))

    # The text index has silently pointed at the wrong fields before, and the
    # only symptom was free-text search returning nothing at all.
    try:
        idx = db.articles.index_information()
        txt = [(n, s) for n, s in idx.items()
               if any(k[0] in ("_fts", "_ftsx") for k in s.get("key", []))]
        if not txt:
            print("%s no text index on articles -- free-text search will return "
                  "nothing. Created automatically on next Corpus()." % WARN)
        else:
            fields = list(txt[0][1].get("weights") or {})
            good = fields == ["full_text"]
            print("%s text index              %s over %s"
                  % (OK if good else BAD, txt[0][0], fields))
            if not good:
                print("       expected ['full_text']. Mongo allows one text index "
                      "per collection and silently ignores create_index when one "
                      "already exists, so this must be dropped to be rebuilt:")
                print("       db.articles.dropIndex(%r)" % txt[0][0])
        print("%s indexes                 %d on articles" % (OK, len(idx)))
    except Exception as ex:
        print("%s index check: %s" % (WARN, str(ex)[:100]))


def write_probe(client, name):
    """Round-trip one throwaway document. Removed whether or not it succeeds."""
    print("\nwrite access")
    col = client[name]["_db_check"]
    try:
        r = col.insert_one({"at": dt.datetime.now(), "by": "scripts/db_check.py"})
        col.delete_one({"_id": r.inserted_id})
        print("%s insert + delete" % OK)
    except mongo_errors.OperationFailure as ex:
        print("%s insert refused: %s" % (BAD, str(ex)[:140]))
        print("       the user can read %r but not write to it" % name)
    except Exception as ex:
        print("%s insert failed: %s" % (BAD, str(ex)[:140]))
    finally:
        try:
            col.drop()
        except Exception:
            pass


def explain(ex, uri):
    """Map the exception onto the thing that actually caused it."""
    atlas = config.is_atlas(uri)
    if isinstance(ex, mongo_errors.ServerSelectionTimeoutError):
        print("       timed out reaching the server.")
        if atlas:
            print("       On Atlas this is almost always the IP access list, not "
                  "the password -- an IP that is not allowed gets a timeout, "
                  "not a permission error.")
            print("       Atlas -> Network Access -> Add IP Address -> Add "
                  "Current IP Address. Allow a minute for it to apply.")
            print("       Also worth ruling out: a corporate network or VPN "
                  "blocking outbound 27017.")
        else:
            print("       Is mongod running?   Get-Service MongoDB")
    elif isinstance(ex, mongo_errors.OperationFailure) and ex.code == 18:
        print("       authentication failed -- wrong username or password.")
        print("       If the password contains @ : / ? # [ ] or %%, it must be "
              "percent-encoded in the URI. '@' becomes '%%40'.")
        print("       Check authSource too: an Atlas database user lives in "
              "'admin', so the URI needs authSource=admin.")
    elif isinstance(ex, mongo_errors.ConfigurationError):
        print("       could not resolve the connection string.")
        print("       For mongodb+srv:// this is DNS. Confirm the cluster "
              "hostname, and that dnspython is installed:  pip install dnspython")
    elif isinstance(ex, mongo_errors.InvalidURI):
        print("       the URI is malformed. Copy it fresh from "
              "Atlas -> Connect -> Drivers.")
    else:
        print("       %s: %s" % (type(ex).__name__, str(ex)[:300]))
    print("\n       current setting: %s" % config.redact(uri))


if __name__ == "__main__":
    sys.exit(main())
