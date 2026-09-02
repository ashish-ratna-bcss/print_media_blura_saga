#!/usr/bin/env python3
"""
Back up the article corpus to S3.

    python scripts/backup.py --dry-run      # check everything, write nothing
    python scripts/backup.py                # dump, verify, upload, prune
    python scripts/backup.py --keep-local   # leave the archive on disk too
    python scripts/backup.py --list         # what is already in the bucket

Connection details come from `.env` through epaper/config.py, the same source
Corpus and every script read. A backup tool that is configured separately from
the thing it backs up will eventually back up the wrong database and nobody
will notice until a restore.

WHY THIS IS SHAPED THE WAY IT IS
--------------------------------
The predecessor to this script produced three backup files of exactly twenty
bytes -- an empty gzip -- and logged a tick beside each one. Every rule below
exists because of a specific way that happened:

  VERIFY, DO NOT ASSUME    A file existing is not a backup. mongodump can fail
                           and still leave a file, and gzip will faithfully
                           compress an empty stream into a valid archive. So
                           the exit code is checked, the documents written are
                           counted, and the archive is read back and inspected
                           before anything is called a success.

  NEVER DELETE THE ONLY    The local file is removed only after the upload is
  COPY                     confirmed. The old script deleted it in a `finally`
                           regardless, so a failed upload destroyed the backup
                           it had just failed to store.

  FAIL LOUDLY              The exit code is the truth. The old `run()` returned
                           True even when the upload had failed, so a cron
                           entry recorded success forever.

  RESTORE IS PART OF IT    `--restore` lives in this same file, so the format
                           written and the format read cannot drift apart. A
                           backup nobody has restored is a hypothesis; the
                           test suite round-trips a real corpus through it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import config                                          # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# A size floor is a guess, and it is only needed where nothing better exists.
# mongodump writes an opaque archive, so an empty result can only be spotted
# by its size; an empty gzip is 20 bytes and a gzipped error message a few
# hundred, so this sits above both.
#
# It is deliberately NOT applied to the python engine, which counts the
# documents it wrote and can therefore say "zero" exactly. Set at 4096 for
# both at first, which rejected a real three-document database -- a byte floor
# cannot tell "empty" from "small", and only one of those is a failure.
MIN_MONGODUMP_BYTES = 512

DEFAULT_BUCKET = os.environ.get("EPAPER_BACKUP_BUCKET", "")
DEFAULT_PREFIX = os.environ.get("EPAPER_BACKUP_PREFIX", "epaper-corpus")
DEFAULT_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
RETENTION_DAYS = int(os.environ.get("EPAPER_BACKUP_RETENTION_DAYS", "14"))


def log(msg=""):
    print(msg, flush=True)


# ----------------------------------------------------------------- checks

def preflight(bucket: str, *, need_s3: bool, engine: str) -> list:
    """
    Everything that must be true, checked before a long dump is paid for.

    Returned as a list of problems rather than raised one at a time, so a first
    run reports every missing piece at once instead of one per attempt.
    """
    problems = []

    if engine == "mongodump" and not shutil.which("mongodump"):
        problems.append(
            "mongodump is not on PATH. Either install the MongoDB Database "
            "Tools:\n"
            "     https://www.mongodb.com/try/download/database-tools\n"
            "  or run with --engine python, which needs nothing beyond pymongo.")

    try:
        from pymongo import MongoClient
        MongoClient(config.mongo_uri(), serverSelectionTimeoutMS=4000,
                    **{k: v for k, v in config.mongo_kwargs().items()
                       if k != "serverSelectionTimeoutMS"}
                    ).admin.command("ping")
    except Exception as ex:
        problems.append("cannot reach MongoDB at %s: %s"
                        % (config.hosts(config.mongo_uri()), str(ex)[:140]))

    if need_s3:
        if not bucket:
            problems.append(
                "no S3 bucket. Pass --bucket, or set EPAPER_BACKUP_BUCKET "
                "in .env")
        try:
            import boto3                                           # noqa: F401
        except ImportError:
            problems.append("boto3 is not installed. pip install boto3")
    return problems


def s3_client(region: str):
    import boto3
    return boto3.client("s3", region_name=region)


def check_credentials(client, bucket: str) -> str:
    """
    Prove we can actually write to the bucket, before dumping anything.

    The old script discovered its credentials were wrong only after producing
    a backup, at which point the failure looked like an upload problem rather
    than a setup problem. Cheap to check first, and the error it returns names
    the cause instead of the symptom.
    """
    try:
        client.head_bucket(Bucket=bucket)
        return ""
    except Exception as ex:
        code = getattr(ex, "response", {}).get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket"):
            return "bucket %r does not exist in this region" % bucket
        if code in ("403", "AccessDenied"):
            return ("credentials are valid but cannot access %r -- check the "
                    "IAM policy allows s3:PutObject on it" % bucket)
        return "cannot reach S3: %s" % str(ex)[:160]


# ------------------------------------------------------------------ dump

def dump(dest: str) -> int:
    """
    mongodump the corpus into one gzipped archive. Returns its size in bytes.

    Raises on anything that would leave a file that is not a restorable
    backup, which is the whole point: the caller must not have to inspect the
    result to find out whether it worked.
    """
    uri, name = config.mongo_uri(), config.mongo_db()

    # The URI goes in argv, and on a host with other users that means a
    # password in `ps`. Stated rather than hidden: mongodump's alternatives
    # (--config, a password prompt) are awkward to drive from a scheduled job,
    # and the honest mitigation is a backup-only MongoDB user with read-only
    # rights, not a cleverer way to pass the same secret.
    cmd = ["mongodump", "--uri=%s" % uri, "--db=%s" % name,
           "--archive=%s" % dest, "--gzip"]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.returncode != 0:
        raise RuntimeError("mongodump exited %d: %s"
                           % (proc.returncode,
                              (proc.stderr or proc.stdout or "").strip()[:400]))

    if not os.path.exists(dest):
        raise RuntimeError("mongodump reported success but wrote no file")

    size = os.path.getsize(dest)
    if size < MIN_MONGODUMP_BYTES:
        raise RuntimeError(
            "archive is only %d bytes, which is not a backup -- an empty gzip "
            "is 20. mongodump said: %s"
            % (size, (proc.stderr or "").strip()[:300] or "(nothing)"))
    return size


def dump_python(dest: str, *, progress=True) -> int:
    """
    Dump every collection with pymongo alone. Returns the archive size.

    The fallback for a machine without the MongoDB Database Tools, which is
    most machines -- mongodump ships separately from the server and is not
    installed here. A backup script that cannot run on the host it protects is
    not a backup script, so this exists rather than an instruction to go and
    install something.

    Format is gzipped JSON lines, one document per line, tagged with its
    collection, encoded through bson.json_util so ObjectIds and datetimes
    survive the round trip exactly. Not mongodump's format and not
    interchangeable with it -- restore with `--restore`, which is in this same
    file precisely so the two cannot drift apart.
    """
    from bson import json_util
    from pymongo import MongoClient

    db = MongoClient(config.mongo_uri(),
                     **config.mongo_kwargs())[config.mongo_db()]
    names = sorted(db.list_collection_names())
    if not names:
        raise RuntimeError("database %r holds no collections" % config.mongo_db())

    written = 0
    with gzip.open(dest, "wt", encoding="utf-8") as fh:
        # A header line, so a restore can refuse an archive from another
        # database or a future format rather than half-applying it.
        fh.write(json_util.dumps({
            "_meta": {"format": "epaper-jsonl-1", "database": config.mongo_db(),
                      "host": config.hosts(config.mongo_uri()),
                      "collections": names,
                      "taken": dt.datetime.now().isoformat()}}) + "\n")
        for name in names:
            n = 0
            for doc in db[name].find({}):
                fh.write(json_util.dumps({"_c": name, "_d": doc}) + "\n")
                n += 1
            written += n
            if progress:
                log("   %-18s %6d documents" % (name, n))

    # No size floor here. The count is exact, so "did this dump anything" is
    # answerable without guessing, and a small database is not a failure.
    if not written:
        raise RuntimeError("database %r holds collections but no documents"
                           % config.mongo_db())
    return os.path.getsize(dest)


def restore(path: str, *, target_db: str, drop: bool, dry: bool) -> dict:
    """
    Put a python-engine archive back. The half that makes the backup real.

    Refuses by default to write into a database that already holds documents:
    restoring over a live corpus is almost always a mistake, and `--drop` is
    how somebody says they meant it.
    """
    from bson import json_util
    from pymongo import MongoClient

    db = MongoClient(config.mongo_uri(), **config.mongo_kwargs())[target_db]

    with gzip.open(path, "rt", encoding="utf-8") as fh:
        header = json_util.loads(fh.readline() or "{}").get("_meta") or {}
        if header.get("format") != "epaper-jsonl-1":
            raise RuntimeError(
                "not an archive this can restore (format=%r). A mongodump "
                "archive is restored with `mongorestore --gzip --archive=...`"
                % header.get("format"))

        log("archive     taken %s from %s / %s"
            % (header.get("taken", "?"), header.get("host", "?"),
               header.get("database", "?")))
        log("restoring into %s / %s" % (config.hosts(config.mongo_uri()),
                                        target_db))

        existing = {c: db[c].estimated_document_count()
                    for c in db.list_collection_names()}
        occupied = {c: n for c, n in existing.items() if n}
        if occupied and not drop and not dry:
            raise RuntimeError(
                "%s already holds documents (%s). Re-run with --drop to "
                "replace them, or --restore-into a different database name."
                % (target_db,
                   ", ".join("%s=%d" % kv for kv in list(occupied.items())[:4])))

        if drop and not dry:
            for c in header.get("collections", []):
                db[c].drop()
            log("dropped %d collection(s)" % len(header.get("collections", [])))

        counts, batch, current = {}, [], None
        for line in fh:
            row = json_util.loads(line)
            name, doc = row.get("_c"), row.get("_d")
            if not name or doc is None:
                continue
            if name != current and batch:
                if not dry:
                    db[current].insert_many(batch, ordered=False)
                batch = []
            current = name
            batch.append(doc)
            counts[name] = counts.get(name, 0) + 1
            if len(batch) >= 1000:
                if not dry:
                    db[current].insert_many(batch, ordered=False)
                batch = []
        if batch and not dry:
            db[current].insert_many(batch, ordered=False)

    return {"collections": counts, "total": sum(counts.values()),
            "dry_run": dry}


def verify(path: str) -> dict:
    """
    Read the archive back and confirm it holds the collections we expect.

    The step the old script had no equivalent of. A file of the right size can
    still be truncated or be a gzipped error page, and the moment to discover
    that is now -- not during a restore, six weeks later, with the source gone.
    """
    try:
        with gzip.open(path, "rb") as fh:
            head = fh.read(200000)
    except Exception as ex:
        raise RuntimeError("archive is not readable gzip: %s" % str(ex)[:160])

    if not head:
        raise RuntimeError("archive decompresses to nothing")

    # Both engines are checked the same way: read the start back and look for
    # the collection names. mongodump embeds them as BSON metadata, the python
    # engine writes them into its header line, and in both cases their absence
    # means the archive is not a dump of this corpus.
    found = [c for c in (b"articles", b"categories", b"epapers", b"pages",
                         b"sources", b"beats")
             if c in head]
    if b"articles" not in head:
        raise RuntimeError(
            "archive does not mention the `articles` collection -- it may be "
            "a dump of the wrong database. Found: %s"
            % (", ".join(c.decode() for c in found) or "nothing recognisable"))
    return {"collections": [c.decode() for c in found],
            "bytes": os.path.getsize(path)}


# ------------------------------------------------------------------- s3

def upload(client, path: str, bucket: str, prefix: str, stamp: str) -> str:
    key = "%s/%s/%s" % (prefix.strip("/"), stamp[:8], os.path.basename(path))
    client.upload_file(
        path, bucket, key,
        ExtraArgs={"StorageClass": "STANDARD_IA",
                   "Metadata": {"database": config.mongo_db(),
                                "host": config.hosts(config.mongo_uri()),
                                "taken": dt.datetime.now().isoformat()}})
    # Confirm it is actually there and the right size. upload_file raising
    # nothing is not the same as the object existing.
    head = client.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != os.path.getsize(path):
        raise RuntimeError("uploaded object is %d bytes, local file is %d"
                           % (head["ContentLength"], os.path.getsize(path)))
    return key


def each_object(client, bucket: str, prefix: str):
    """
    Every object under the prefix, across pages.

    list_objects_v2 returns at most 1000 keys. The old cleanup called it once,
    so on a bucket with more than that, the oldest backups -- the ones it
    existed to delete -- were never reached.
    """
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix.strip("/") + "/"}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            yield obj
        if not page.get("IsTruncated"):
            return
        token = page.get("NextContinuationToken")


def prune(client, bucket: str, prefix: str, days: int, *, dry: bool) -> int:
    """Delete backups older than the retention window. Never the newest one."""
    cutoff = dt.datetime.now() - dt.timedelta(days=days)
    objs = list(each_object(client, bucket, prefix))
    if not objs:
        return 0

    # Whatever the retention rule says, one backup always survives. A clock
    # skew or a long gap in collection must not be able to empty the bucket.
    newest = max(objs, key=lambda o: o["LastModified"])["Key"]

    removed = 0
    for obj in objs:
        if obj["Key"] == newest:
            continue
        when = obj["LastModified"].replace(tzinfo=None)
        if when >= cutoff:
            continue
        if dry:
            log("   would delete %s (%s)" % (obj["Key"], when.date()))
        else:
            client.delete_object(Bucket=bucket, Key=obj["Key"])
            log("   deleted %s (%s)" % (obj["Key"], when.date()))
        removed += 1
    return removed


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default=DEFAULT_BUCKET)
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    ap.add_argument("--dry-run", action="store_true",
                    help="check everything and dump, but upload nothing")
    ap.add_argument("--keep-local", action="store_true",
                    help="keep the archive on disk after a successful upload")
    ap.add_argument("--out", help="where to write the archive (default: temp)")
    ap.add_argument("--no-upload", action="store_true",
                    help="dump and verify only; do not touch S3")
    ap.add_argument("--list", action="store_true",
                    help="list what is already in the bucket and exit")
    ap.add_argument("--engine", choices=("auto", "mongodump", "python"),
                    default="auto",
                    help="auto uses mongodump when present, else python")
    ap.add_argument("--restore", metavar="ARCHIVE",
                    help="restore a python-engine archive instead of backing up")
    ap.add_argument("--restore-into", metavar="DB",
                    help="restore into this database (default: the configured one)")
    ap.add_argument("--drop", action="store_true",
                    help="with --restore, replace collections that already exist")
    args = ap.parse_args()

    config.load_env()

    if args.restore:
        try:
            r = restore(args.restore,
                        target_db=args.restore_into or config.mongo_db(),
                        drop=args.drop, dry=args.dry_run)
        except Exception as ex:
            log("RESTORE FAILED: %s" % ex)
            return 1
        log()
        for name, n in sorted(r["collections"].items()):
            log("   %-18s %6d documents" % (name, n))
        log("\n%s %d documents"
            % ("would restore" if r["dry_run"] else "restored", r["total"]))
        return 0

    engine = args.engine
    if engine == "auto":
        engine = "mongodump" if shutil.which("mongodump") else "python"
    want_s3 = not args.no_upload
    log("database   %s / %s" % (config.hosts(config.mongo_uri()),
                                config.mongo_db()))
    if want_s3:
        log("bucket     s3://%s/%s" % (args.bucket or "(unset)", args.prefix))
    log()

    if args.list:
        if not args.bucket:
            log("no bucket configured")
            return 2
        client = s3_client(args.region)
        rows = sorted(each_object(client, args.bucket, args.prefix),
                      key=lambda o: o["LastModified"], reverse=True)
        if not rows:
            log("bucket holds no backups under this prefix")
            return 0
        log("%-58s %10s  %s" % ("KEY", "BYTES", "TAKEN"))
        for o in rows[:50]:
            log("%-58s %10d  %s" % (o["Key"][:58], o["Size"],
                                    o["LastModified"].strftime("%Y-%m-%d %H:%M")))
        return 0

    problems = preflight(args.bucket, need_s3=want_s3, engine=engine)
    if problems:
        log("cannot back up:")
        for p in problems:
            log("   - %s" % p)
        return 2

    client = None
    if want_s3:
        client = s3_client(args.region)
        # Checked BEFORE the dump. Discovering bad credentials afterwards is
        # what turned a setup problem into a lost backup last time.
        err = check_credentials(client, args.bucket)
        if err:
            log("cannot back up: %s" % err)
            return 2

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = "%s_%s.archive.gz" % (config.mongo_db(), stamp)
    tmpdir = None
    if args.out:
        path = args.out
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    else:
        tmpdir = tempfile.mkdtemp(prefix="epaper-backup-")
        path = os.path.join(tmpdir, name)

    uploaded = False
    try:
        log("dumping with the %s engine ..." % engine)
        size = dump(path) if engine == "mongodump" else dump_python(path)
        log("   %s  (%.1f MB)" % (os.path.basename(path), size / 1048576.0))

        log("verifying ...")
        info = verify(path)
        log("   readable, collections seen: %s" % ", ".join(info["collections"]))

        if args.dry_run or args.no_upload:
            log("\n%s -- archive left at:\n   %s"
                % ("dry run" if args.dry_run else "upload skipped", path))
            # Nothing was uploaded, so the local file is the only copy and is
            # kept whatever --keep-local says.
            tmpdir = None
            return 0

        log("uploading ...")
        key = upload(client, path, args.bucket, args.prefix, stamp)
        uploaded = True
        log("   s3://%s/%s" % (args.bucket, key))

        if args.retention_days > 0:
            log("pruning backups older than %d days ..." % args.retention_days)
            n = prune(client, args.bucket, args.prefix, args.retention_days,
                      dry=False)
            log("   %d removed" % n)

        log("\nbackup complete")
        return 0

    except Exception as ex:
        log("\nBACKUP FAILED: %s" % ex)
        if os.path.exists(path):
            # The archive stays. It may be partial and unusable, but deciding
            # that is the operator's call, and deleting the only artefact of a
            # failed run destroys the evidence of why it failed.
            log("the archive was left at %s for inspection" % path)
            tmpdir = None
        return 1

    finally:
        # Only ever after a confirmed upload, and only the temp copy.
        if uploaded and tmpdir and not args.keep_local:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
