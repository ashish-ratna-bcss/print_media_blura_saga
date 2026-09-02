#!/usr/bin/env python3
"""
Collect every registered paper for one day. The unattended nightly run.

    python scripts/daily.py                    # today
    python scripts/daily.py --date 2026-08-31
    python scripts/daily.py --dry-run          # say what it would collect
    python scripts/daily.py --backfill 7       # the last 7 days, oldest first

No scheduler is embedded. This is a plain command that does one night's work
and exits, which Task Scheduler on Windows or cron elsewhere can call. That is
deliberate: an in-process scheduler dies with the web app, gives no way to run
a night by hand, and turns "did last night work" into a question about a
long-lived process rather than an exit code somebody can look at.

What it collects comes from the `sources` collection, not from arguments, so
adding a paper to the nightly run is a POST to /api/sources rather than an
edit to a crontab on a machine somebody has to find.

Three properties matter, all of them about running with nobody watching:

  IDEMPOTENT  A title already collected for the date is skipped. Running twice
              costs two database reads. This is why `sources.due()` compares a
              stored date rather than a timestamp -- a run at 02:00 and a
              re-run at 06:00 do the same thing.

  RESUMABLE   Within a title, pages already held are skipped by the edition
              job itself, so an interrupted night resumes at the page it
              stopped on rather than re-reading an edition.

  PARTIAL     One paper failing does not stop the others. An e-paper that has
              not published by 02:00 is the normal case, not an error, and it
              is recorded against that title so a paper failing quietly every
              night is visible in `last_error` and `fail_count`.

Exit code is 0 when every due title was collected, 1 when any failed -- so the
scheduler's own history is a usable record without reading the log.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from epaper import config, sources as src_mod, vlm                 # noqa: E402

# Windows defaults stdout to cp1252, which cannot encode Devanagari. Printing
# a Hindi paper name or headline would raise UnicodeEncodeError and kill the
# run -- the same defaulting that once broke every Hindi page upload.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def collect(spec: dict, date: str, *, force: bool = False) -> dict:
    """
    One title for one date, start to finish, synchronously.

    Imports the web app because that is where the job machinery lives. Done
    inside the function so `--dry-run` and `--list` work on a machine that
    cannot import torch.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))
    import app as web                                              # noqa: E402

    # Importing the app does not run its startup hook, so the job schema may be
    # older than the code. Idempotent, and cheap enough to do every night --
    # skipping it means the run fails on the first write to a column added
    # since the database was created.
    web.init_db()

    jid = web._create({"source": "ingest"}, spec.get("paper") or spec["host"],
                      spec.get("edition", ""), date, 0, True)
    web.set_job(jid, kind="edition")
    web.run_ingest_job(jid, {
        "source": "ingest", "host": spec["host"],
        "title_id": spec["title_id"], "date": date,
        "paper": spec.get("paper") or spec["host"],
        "edition": spec.get("edition", ""),
        "max_pages": int(spec.get("max_pages") or 0), "force": force})

    return web.jobs_store().get(jid) or {"id": jid, "status": "unknown"}


def collect_with_retry(store, spec: dict, date: str, *, force=False,
                       retries: int = 2, wait: int = 300) -> dict:
    """
    One title, retried on failure with a wait between attempts.

    The failures worth retrying are the ones that pass on their own: an
    edition not published yet at 02:00, a CDN timeout, a model server still
    loading. Retrying immediately would hit exactly the same state, so the
    wait is minutes rather than seconds -- long enough for the cause to have
    changed, short enough to finish inside a night.

    Retries are bounded, because a title whose e-paper simply does not exist
    for this date will fail identically forever, and an unbounded retry turns
    one dead paper into a run that never reaches the others.
    """
    last = {}
    for attempt in range(1, retries + 2):
        try:
            job = collect(spec, date, force=force)
            if job.get("status") == "done":
                return job
            last = job
            reason = (job.get("error") or "job did not finish")[:120]
        except Exception as ex:
            last = {"status": "error", "error": str(ex)}
            reason = str(ex)[:120]

        if attempt <= retries:
            print("      attempt %d/%d failed: %s"
                  % (attempt, retries + 1, reason))
            print("      waiting %ds before retrying" % wait, flush=True)
            time.sleep(wait)
        else:
            print("      attempt %d/%d failed: %s"
                  % (attempt, retries + 1, reason))
    return last or {"status": "error", "error": "no attempt completed"}


def run_one_day(store, date: str, *, dry_run=False, force=False,
                retries: int = 2, wait: int = 300) -> int:
    due = store.due(date) if not force else store.listing(enabled_only=True)
    print("\n=== %s ===" % date)
    if not due:
        print("   nothing due -- every enabled title already has this date")
        return 0

    print("   %d title(s) to collect" % len(due))
    if dry_run:
        for s in due:
            print("      would collect %-28s %s" % (s["paper"], s["host"]))
        return 0

    failed = 0
    for s in due:
        t0 = time.time()
        print("\n   %-28s %s" % (s["paper"], s["host"]), flush=True)
        job = collect_with_retry(store, s, date, force=force,
                                 retries=retries, wait=wait)
        el = time.time() - t0
        if job.get("status") == "done":
            store.mark_done(s["id"], date,
                            pages=job.get("pages_done") or 0,
                            articles=job.get("n_articles") or 0)
            print("      ok  %s pages, %s articles, %.0fs%s"
                  % (job.get("pages_done"), job.get("n_articles"), el,
                     "  [%s]" % job["note"] if job.get("note") else ""))
        else:
            failed += 1
            store.mark_failed(s["id"], job.get("error") or "job did not finish",
                              date)
            print("      FAILED  %s  (%.0fs)"
                  % ((job.get("error") or "unknown")[:150], el))
    return failed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill", type=int, default=0, metavar="N",
                    help="also collect the N days before --date, oldest first")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be collected, fetch nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-collect even where the date is already recorded")
    ap.add_argument("--list", action="store_true",
                    help="show every registered title and its last collection")
    ap.add_argument("--retries", type=int, default=2,
                    help="extra attempts per title after a failure (default 2)")
    ap.add_argument("--retry-wait", type=int, default=300, metavar="SECONDS",
                    help="wait between attempts (default 300)")
    args = ap.parse_args()

    config.load_env()
    store = src_mod.store()
    if not store:
        print("cannot reach the source list: %s" % src_mod.last_error())
        return 2

    if args.list:
        rows = store.listing()
        if not rows:
            print("no sources registered. Add one with POST /api/sources")
            return 0
        print("%-30s %-34s %-12s %-8s %s"
              % ("PAPER", "HOST", "LAST DATE", "FAILS", "LAST ERROR"))
        for s in rows:
            print("%-30s %-34s %-12s %-8s %s"
                  % (s["paper"][:30], s["host"][:34], s["last_date"] or "-",
                     s.get("fail_count") or 0,
                     (s.get("last_error") or "")[:40]))
        return 0

    base = args.date or dt.date.today().isoformat()
    try:
        d0 = dt.date.fromisoformat(base)
    except ValueError:
        print("--date must be YYYY-MM-DD, got %r" % base)
        return 2

    # Oldest first, so an interrupted backfill leaves a contiguous archive
    # with a gap at the recent end rather than holes scattered through it.
    days = [(d0 - dt.timedelta(days=n)).isoformat()
            for n in range(args.backfill, -1, -1)]

    if not args.dry_run:
        h = vlm.health()
        if not h.get("available"):
            # Checked once, up front. Collecting a whole edition against a
            # dead model server produces pages of empty transcriptions that
            # look like successfully read blank newsprint.
            print("model server unavailable: %s" % (h.get("error") or "?"))
            print("nothing collected. Start the model server and re-run.")
            return 2

    failed = sum(run_one_day(store, d, dry_run=args.dry_run, force=args.force,
                             retries=args.retries, wait=args.retry_wait)
                 for d in days)
    print("\n%s" % ("all titles collected" if not failed
                    else "%d title(s) failed -- see --list for the reasons"
                         % failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
