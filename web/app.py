#!/usr/bin/env python3
"""
E-Paper extraction web app.

    python3 web/app.py --port 8080

Two ways in:
  - upload a page (JPG/PNG, or PDF if pdftoppm is on PATH)
  - pick a newspaper, edition and date from the free Readwhere catalogue

Both land in the same pipeline and the same history. Jobs run on a background
thread and the UI polls, so a slow page never blocks the browser.

State lives in SQLite next to the job files rather than MongoDB: the GPU box
has no pymongo in its venv, and one self-contained directory is far easier to
move between machines.

Nothing here touches the existing extraction code paths -- it imports them.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import urllib.request
import uuid

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# The corpus is half Devanagari and the Windows console is cp1252, which cannot
# encode it. Any print() carrying a Hindi headline -- a progress line, an
# ingest error, a traceback -- would raise UnicodeEncodeError from inside the
# logging rather than from the work, turning a diagnostic into the failure it
# was trying to report. Same root cause as the regions.json crash below;
# fixing it once here covers every print in the process.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass                                  # already UTF-8, or not a tty

from epaper import config as epaper_config     # noqa: E402
from epaper import beats as beats_mod          # noqa: E402
from epaper import catdata                        # noqa: E402
from epaper import categories as cat_mod          # noqa: E402
from epaper import classify                       # noqa: E402
from epaper import fragments                      # noqa: E402
from epaper import jobs as jobstore                # noqa: E402
from epaper import query as epaper_query           # noqa: E402
from epaper import sources as src_mod             # noqa: E402
from epaper import corpus as corpus_mod, keywords, local_backend, topics, vlm  # noqa: E402

# Before anything reads os.environ. A `.env` at the repo root is how the
# database is configured; loading it after the first getenv would be a
# race that works right up until it doesn't.
epaper_config.load_env()

# Where the active vocabulary came from. Set at startup, and again after any
# edit. Reported by /api/beats/{id} so it is never a guess whether the running
# process is serving what MongoDB holds or the built-in fallback.
_beats_source = "built-in"
from epaper import probe as probe_mod             # noqa: E402
from epaper.platforms.livehindustan import LiveHindustan  # noqa: E402
from epaper.platforms.summit import Summit        # noqa: E402
from epaper.acquire import download as acq_download, stitch_tiles  # noqa: E402
from epaper.platforms.readwhere import Readwhere  # noqa: E402
from epaper.segment import crop_region           # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
JOBS = os.path.join(DATA, "jobs")
# The SQLite file job state used to live in. Kept as a constant only so
# scripts/migrate_jobs.py has one place to find it; nothing here reads it any
# more. Safe to delete once the History tab has been checked against MongoDB.
LEGACY_DB = os.path.join(DATA, "jobs.db")

# The Windows Task Scheduler entry that runs the nightly collection. Named
# rather than generated so registering twice replaces the task instead of
# leaving two of them fetching the same editions at the same time.
SCHEDULED_TASK = "ClippingDeskDailyCollect"
os.makedirs(JOBS, exist_ok=True)

Image.MAX_IMAGE_PIXELS = None


@contextlib.asynccontextmanager
async def lifespan(_app):
    """Open the job store, then install the stored beat vocabulary."""
    # init_db() used to run only from main(), which meant it happened when the
    # app was LAUNCHED and never when it was IMPORTED. The nightly runner
    # imports it -- scripts/daily.py drives run_ingest_job directly -- so it
    # started against a store it had never opened. Found by running a real
    # edition; no unit test would have, because they never touch the database.
    init_db()
    _load_beats()
    yield


app = FastAPI(title="E-Paper Extraction", lifespan=lifespan)
_lock = threading.Lock()

# Two kinds of source, and the difference decides how good topic search can be.
#   summit    - the publisher serves its own article text. No OCR, exact words,
#               whole edition in seconds. Topic search here is fast and precise.
#   readwhere - page images only. Every article has to be found and read first,
#               so topic search costs minutes per page.
#
# `states` is where each title actually circulates, as beat state codes. It is
# what stops the pairing the UI used to allow without comment: Jharkhand Police
# against the Chandigarh Tribune, which is a five-minute job guaranteed to find
# nothing. A `national` title is offered for every force.
CATALOGUE = [
    {"name": "Sakshi", "host": "epaper.sakshi.com", "lang": "te",
     "platform": "summit", "states": ["AP", "TG"]},
    {"name": "Namasthe Telangana", "host": "epaper.ntnews.com", "lang": "te",
     "platform": "summit", "states": ["TG", "AP"]},
    {"name": "The Tribune", "host": "epaper.tribuneindia.com", "lang": "en",
     "platform": "readwhere", "states": ["PB", "HR", "HP", "CH", "DL", "JK"]},
    {"name": "Dainik Tribune", "host": "epaper.dainiktribuneonline.com",
     "lang": "hi", "platform": "readwhere",
     "states": ["PB", "HR", "HP", "CH", "DL"]},
    {"name": "Jansatta", "host": "epaper.jansatta.com", "lang": "hi",
     "platform": "readwhere", "states": ["DL", "UP", "WB", "CG", "CH"]},
    {"name": "Financial Express", "host": "epaper.financialexpress.com",
     "lang": "en", "platform": "readwhere", "states": [], "national": True},
    {"name": "The Statesman", "host": "epaper.thestatesman.com", "lang": "en",
     "platform": "readwhere", "states": ["WB", "DL", "OD"]},
    # Added for coverage: the beats reach all 36 states while the catalogue
    # reached about a third. Hindustan is UP, Bihar and Delhi, and at
    # 2438x4000 it is the highest-resolution source here -- larger than
    # Readwhere's stitched 2400px tiles, from a single image with no login.
    {"name": "Hindustan", "host": "epaper.livehindustan.com", "lang": "hi",
     "platform": "livehindustan", "states": ["UP", "BR", "DL"]},
]


def platform_of(host):
    return next((p["platform"] for p in CATALOGUE if p["host"] == host), "readwhere")


def lang_of(host):
    """
    The language this title is printed in, for the OCR engine.

    The catalogue already records it, and headline recognition is the one place
    that has to know: `eng+hin` over an English broadsheet lets Tesseract pick
    Devanagari for Latin type, which it did -- a Tribune headline came back as
    "ol cr for एम र नि". Unknown hosts (uploads) return "" and keep the
    configured default.
    """
    return next((p.get("lang", "") for p in CATALOGUE if p["host"] == host), "")
_cache = {}


# ---------------------------------------------------------------- storage

def init_db():
    """
    Prepare the job store.

    Was a SQLite schema migration -- CREATE TABLE plus an ALTER for every
    column added since -- and is now index creation, because MongoDB has no
    schema to migrate. Adding a field to a job is writing it; the defaults in
    epaper/jobs.py are what keep older documents answering with a 0 where the
    frontend expects one rather than an undefined.

    Still called on startup, and still tolerant: a database that is down is
    reported by the first request that needs it, with the reason, rather than
    preventing the process from starting at all.
    """
    if not jobstore.store():
        print("job store unavailable at startup: %s" % jobstore.last_error())


def jobs_store():
    """
    The job store, or a 503 naming the reason.

    Job state lives in MongoDB with the corpus (see epaper/jobs.py), which
    makes the database a hard requirement rather than the optional index it
    was: without it there is nowhere to record that a run started, so the
    honest thing is to refuse the request rather than begin work whose result
    cannot be kept.
    """
    s = jobstore.store()
    if not s:
        raise HTTPException(503, "job store unavailable: %s"
                            % (jobstore.last_error() or "mongodb down"))
    return s


def set_job(jid, **kw):
    if not kw:
        return
    s = jobstore.store()
    if s:
        s.set(jid, **kw)


def job_dir(jid):
    d = os.path.join(JOBS, jid)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------- corpus
#
# MongoDB holds the searchable corpus; SQLite keeps job state. They answer
# different questions -- "how did this run go" versus "what has this newspaper
# ever printed about the Delhi Police" -- and only the second needs to outlive
# the job that produced it.
#
# Ingestion is best-effort on purpose. A Mongo that is down must not fail an
# extraction that otherwise succeeded: the articles are already on disk and in
# SQLite, and the corpus can be backfilled from those at any time.

_corpus = None
_corpus_err = ""


def corpus():
    """
    The corpus handle, or None with the reason recorded in _corpus_err.

    No connection details here on purpose -- epaper.config owns them, reads
    `.env`, and is the single thing to change when this moves from a local
    mongod to Atlas.
    """
    global _corpus, _corpus_err
    if _corpus is None:
        try:
            c = corpus_mod.Corpus()
            if not c.ping():
                raise RuntimeError(
                    "mongodb not reachable at %s"
                    % epaper_config.hosts(epaper_config.mongo_uri()))
            _corpus, _corpus_err = c, ""
        except Exception as ex:
            _corpus_err = str(ex)[:200]
            return None
    return _corpus


def ingest_quietly(articles, **kw):
    """Index a job's output. Never raises -- see the note above."""
    c = corpus()
    if not c:
        print("corpus unavailable, not indexed: %s" % _corpus_err)
        return {"added": 0, "updated": 0, "skipped": 0, "error": _corpus_err}
    try:
        r = c.ingest(articles, **kw)
        print("corpus: +%d new, %d updated, %d skipped, %d failed"
              % (r["added"], r["updated"], r["skipped"], r.get("failed", 0)))
        for e in (r.get("errors") or [])[:5]:
            print("  corpus write failed -- %s" % e)
        return r
    except Exception as ex:
        traceback.print_exc()
        return {"added": 0, "updated": 0, "skipped": 0, "error": str(ex)[:200]}


# ---------------------------------------------------------------- pipeline

def to_image(src_path, dest):
    """Normalise an upload to a JPEG page. PDFs go through pdftoppm if present."""
    if src_path.lower().endswith(".pdf"):
        if not shutil.which("pdftoppm"):
            raise RuntimeError("PDF upload needs poppler (pdftoppm) on PATH; "
                               "upload a JPG or PNG instead")
        out = os.path.join(os.path.dirname(dest), "pdfpage")
        subprocess.run(["pdftoppm", "-jpeg", "-r", "200", "-f", "1", "-l", "1",
                        src_path, out], check=True, capture_output=True)
        made = [f for f in os.listdir(os.path.dirname(dest))
                if f.startswith("pdfpage")]
        if not made:
            raise RuntimeError("could not render the PDF's first page")
        os.replace(os.path.join(os.path.dirname(dest), made[0]), dest)
        return dest
    with Image.open(src_path) as im:
        im.convert("RGB").save(dest, "JPEG", quality=92)
    return dest


def fetch_page(host, title_id, date, page_no, dest):
    """
    Pull one page as an image, whichever platform serves it.

    Readwhere needs a tile pyramid stitched; Hindustan serves the whole page as
    one file, at higher resolution than the stitched result.
    """
    plat = platform_of(host)
    if plat == "livehindustan":
        return fetch_page_lh(host, title_id, date, page_no, dest)
    if plat == "summit":
        return fetch_page_summit(host, title_id, date, page_no, dest)
    rw = Readwhere(host)
    issue = rw.issue_for_date(title_id, date)
    if not issue:
        raise RuntimeError("no issue published for %s on %s" % (title_id, date))
    cfg = rw.issue_config(issue)
    n = cfg.get("numPages", 0)
    meta = rw.page_meta(issue, page_no, page_no)
    # Exact key only. Readwhere answers a request for a GATED page by returning
    # page 1 rather than an error -- confirmed against three hosts: Financial
    # Express (free) answers `2-2` with key '2', while The Tribune and Dainik
    # Tribune answer it with key '1' and page 1's tile pyramid.
    #
    # This used to fall back to `list(meta.values())[0]`, which on a
    # single-page request can only fire when the API returned something other
    # than what was asked for. It downloaded page 1 and labelled it page 2, and
    # since everything downstream was correct work on the wrong image, the only
    # visible symptom was a suspiciously round duplicate: 21 articles on page 1
    # and the same 21, byte-identical crops and all, on page 2.
    pm = meta.get(str(page_no)) or meta.get(page_no)
    if not pm:
        served = sorted(meta.keys()) if meta else []
        if served:
            raise RuntimeError(
                "page %s of issue %s was not served -- the publisher returned "
                "page(s) %s instead. This title gates everything past page 1; "
                "pagemeta/get needs a subscriber token for the rest."
                % (page_no, issue, ", ".join(served)))
        raise RuntimeError("page %s unavailable for issue %s" % (page_no, issue))
    level = rw.best_level(pm)
    if not stitch_tiles(rw.tiles(level), level["width"], level["height"], dest):
        raise RuntimeError("page tiles could not be assembled")
    return {"issue_id": issue, "num_pages": n, "title_name": cfg.get("titleName", "")}


def fetch_page_summit(host, edition_id, date, page_no, dest):
    """
    One Summit page as an image.

    Summit serves whole page renders rather than a tile pyramid, and -- unlike
    Readwhere, which returns page 1 for anything gated -- it serves every page
    of the edition. Measured on Sakshi: six requested, six distinct images of
    about 2.4 MB each. So a Telugu title collects a complete newspaper where a
    Tribune edition collects a front page.

    Shares the resolution rule with summit_pages_to_disk: "HighResolution" is
    a misnomer for the ~900px render, at which body text is unreadable to any
    OCR, so the XX variant is taken first.
    """
    pages = Summit(host).pages(
        edition_id, "%s/%s/%s" % (date[8:10], date[5:7], date[0:4]))
    if not pages:
        raise RuntimeError("no issue published for %s on %s" % (edition_id, date))

    want = int(page_no)
    pg = next((p for p in pages if int(p.get("PageNo") or 0) == want), None)
    if pg is None:
        raise RuntimeError("page %d not in this edition (%d pages)"
                           % (want, len(pages)))
    url = (pg.get("XXHighResolution") or pg.get("XHighResolution")
           or pg.get("HighResolution") or pg.get("LowResolution"))
    if not url:
        raise RuntimeError("page %d carries no image url" % want)
    if not acq_download(url, dest):
        raise RuntimeError("page %d could not be downloaded" % want)
    return dest


def fetch_page_lh(host, city, date, page_no, dest):
    """
    One Hindustan page: a single GET, no stitching.

    Saved as JPEG rather than kept as WebP because the rest of the pipeline --
    crop_region, the overlay renderer, the PDF builder -- assumes it can reopen
    what this writes, and WebP support in Pillow is a build-time option that is
    not guaranteed on the GPU box.

    An unpublished issue is reported as such. Hindustan answers with
    totalPages 0 rather than a 404, so without this the caller would see an
    empty page list and no reason for it.
    """
    lh = LiveHindustan(host)
    ed = lh.edition(city, date)
    if not ed["pages"]:
        raise RuntimeError(
            "no issue published for %s on %s (the edition exists but has no "
            "pages yet)" % (ed.get("name") or city, date or "today"))
    url = next((p["url"] for p in ed["pages"]
                if p["page_no"] == int(page_no)), "")
    if not url:
        raise RuntimeError("page %s not in this issue -- %s has %d page(s)"
                           % (page_no, ed.get("name") or city, ed["total_pages"]))
    pw = ed.get("paywall_from")
    if pw and int(page_no) >= int(pw):
        raise RuntimeError("page %s is behind the paywall (free up to page %s)"
                           % (page_no, int(pw) - 1))

    req = urllib.request.Request(
        url, headers={"User-Agent": probe_mod.UA,
                      "Referer": "https://%s/" % host})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    with Image.open(io.BytesIO(raw)) as im:
        im.convert("RGB").save(dest, "JPEG", quality=92)
    return {"issue_id": ed.get("edition_id", ""),
            "num_pages": ed.get("total_pages", 0),
            "title_name": ed.get("name") or city}


def overlay(page_path, regions, dest, max_edge=1500):
    colors = {"article": (27, 127, 212), "ad": (201, 112, 28),
              "masthead": (118, 126, 142)}
    im = Image.open(page_path).convert("RGB")
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    lw = max(3, im.size[0] // 420)
    for r in regions:
        c = colors.get(r.get("type"), (118, 126, 142))
        d.rectangle(r["bbox_px"], fill=c + (26,), outline=c + (255,), width=lw)
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    im.thumbnail((max_edge, max_edge))
    im.save(dest, "JPEG", quality=84)


def run_job(jid, spec):
    """The whole extraction for one page. Runs off-thread."""
    t0 = dt.datetime.now()
    d = job_dir(jid)
    page = os.path.join(d, "page.jpg")
    try:
        set_job(jid, status="running", stage="acquiring")
        if spec["source"] == "upload":
            to_image(spec["tmp"], page)
            if os.path.exists(spec["tmp"]):
                os.remove(spec["tmp"])
        else:
            info = fetch_page(spec["host"], spec["title_id"], spec["date"],
                              spec["page_no"], page)
            set_job(jid, edition=info["title_name"] or spec.get("edition", ""))

        with Image.open(page) as im:
            W, H = im.size
        set_job(jid, width=W, height=H, stage="detecting layout")

        regions = local_backend.detect_regions(
            page, spec.get("page_no") or 1,
            paper=spec.get("paper", "upload"), date=spec.get("date", ""),
            debug=False, lang=lang_of(spec.get("host")))

        set_job(jid, stage="cropping")
        overlay(page, regions, os.path.join(d, "overlay.jpg"))

        arts, seq = [], 0
        for r in regions:
            seq += 1
            name = "a%02d.jpg" % seq
            crop_region(page, r["bbox_px"], os.path.join(d, name))
            arts.append({"seq": seq, "type": r.get("type", "article"),
                         "headline": r.get("headline_visible", ""),
                         "bbox": r["bbox_px"], "crop": name,
                         "region_boxes": r.get("region_boxes") or [],
                         "n_elements": len(r.get("elements") or []),
                         "has_photo": bool(r.get("has_photo"))})

        if spec.get("with_text"):
            total = sum(1 for a in arts if a["type"] == "article")
            done = 0
            for a in arts:
                if a["type"] != "article":
                    continue
                done += 1
                set_job(jid, stage="reading text %d/%d" % (done, total))
                try:
                    f = local_backend.transcribe(os.path.join(d, a["crop"]))
                    a.update({"body": f.get("body") or "",
                              "byline": f.get("byline") or "",
                              "dateline": f.get("dateline") or "",
                              "headline": f.get("headline") or a["headline"]})
                except Exception as ex:
                    a["body"] = "[read failed: %s]" % str(ex)[:120]

            # Datelines, bylines and OCR noise look like articles all the way
            # to here -- they have a box and some text. Marking them now, once
            # the text exists to judge, keeps them out of the classifier queue
            # as well as out of the corpus; they stay in `arts` and on the
            # annotated page, so nothing is hidden by the decision.
            _, furniture = fragments.split(
                [a for a in arts if a["type"] == "article"])
            if furniture:
                print("%s: %d blocks marked as page furniture (%s)"
                      % (jid, len(furniture),
                         ", ".join(sorted({f["fragment_reason"]
                                           for f in furniture}))))

            # Topic labelling, once the text exists to label. Batched, so a
            # page of 20 stories costs two calls rather than twenty, and
            # non-fatal: a failure here leaves the articles unlabelled but
            # still extracted, transcribed and stored.
            todo = [a for a in arts if a["type"] == "article"]
            if todo:
                set_job(jid, stage="labelling %d articles" % len(todo))
                try:
                    for a, r in zip(todo, classify.classify(
                            todo, progress=lambda n, t: set_job(
                                jid, stage="labelling %d/%d" % (n, t)))):
                        a.update({"category": r["category"],
                                  "raw_category": r["raw_category"],
                                  "tags": r["tags"]})
                except Exception as ex:
                    print("classify failed for job %s: %s" % (jid, ex))

        # A single page run owns the job's rows outright, so they are replaced
        # rather than appended to -- a re-run of one page should not leave the
        # previous attempt's rows behind it.
        jobs_store().reset_rows(jid, [
            {"type": a["type"], "headline": a["headline"],
             "body": a.get("body", ""), "byline": a.get("byline", ""),
             "dateline": a.get("dateline", ""),
             "category": a.get("category", ""),
             "bbox": json.dumps(a["bbox"]), "crop": a["crop"],
             "n_elements": a["n_elements"],
             "has_photo": int(a["has_photo"])} for a in arts])
        # encoding="utf-8" is not optional here, and its absence was fatal.
        # Python's open() defaults to the locale encoding, which on Windows is
        # cp1252 -- so `ensure_ascii=False`, added precisely so Devanagari
        # stays readable in this file, is what made writing Devanagari raise
        # UnicodeEncodeError and fail the whole job. Every Hindi page upload
        # died here, after the extraction had already been paid for.
        with open(os.path.join(d, "regions.json"), "w", encoding="utf-8") as fh:
            json.dump(regions, fh, ensure_ascii=False, indent=1, default=str)

        ingest_quietly(arts, job_id=jid, paper=spec.get("paper", ""),
                       edition=spec.get("edition", ""), date=spec.get("date", ""),
                       default_page=spec.get("page_no") or 1,
                       page_images={spec.get("page_no") or 1:
                                    {"path": page, "width": W, "height": H}})

        set_job(jid, status="done", stage="complete", error="",
                n_articles=sum(1 for a in arts if a["type"] == "article"),
                n_ads=sum(1 for a in arts if a["type"] == "ad"),
                coverage=float(regions[0].get("coverage", 0) if regions else 0),
                elapsed=(dt.datetime.now() - t0).total_seconds())
    except Exception as ex:
        traceback.print_exc()
        set_job(jid, status="error", stage="failed", error=str(ex)[:400],
                elapsed=(dt.datetime.now() - t0).total_seconds())


# --------------------------------------------------------- edition ingest

def ingest_one_page(jid, d, spec, pno, n_pages):
    """
    One page of an edition, all the way to the corpus. Returns article count.

    The same sequence run_job performs for a single page, with the reporting
    written for a job that will do this twenty more times. Kept separate from
    run_job rather than folded into it: run_job answers "show me this page",
    keeps its overlay and its crops for inspection, and belongs to a person
    waiting. This belongs to a schedule, at 2am, with nobody watching.
    """
    label = "page %d/%d" % (pno, n_pages)
    page = os.path.join(d, "p%02d.jpg" % pno)

    set_job(jid, stage="%s: fetching" % label)
    fetch_page(spec["host"], spec["title_id"], spec["date"], pno, page)
    with Image.open(page) as im:
        W, H = im.size

    set_job(jid, stage="%s: layout" % label)
    regions = local_backend.detect_regions(
        page, pno, paper=spec.get("paper", ""), date=spec.get("date", ""),
        debug=False, lang=lang_of(spec.get("host")))

    arts = []
    for r in regions:
        if r.get("type") != "article":
            continue
        seq = len(arts) + 1
        crop = "p%02d_a%02d.jpg" % (pno, seq)
        try:
            crop_region(page, r["bbox_px"], os.path.join(d, crop))
        except Exception:
            continue
        arts.append({"seq": seq, "type": "article",
                     "headline": r.get("headline_visible", ""),
                     "bbox": r["bbox_px"], "crop": crop, "page_ref": pno,
                     "n_elements": len(r.get("elements") or []),
                     "has_photo": bool(r.get("has_photo")),
                     "text_source": "vlm"})
    if not arts:
        return 0

    for n, a in enumerate(arts, 1):
        set_job(jid, stage="%s: reading %d/%d" % (label, n, len(arts)))
        try:
            f = local_backend.transcribe(os.path.join(d, a["crop"]))
            a.update({"body": f.get("body") or "",
                      "byline": f.get("byline") or "",
                      "dateline": f.get("dateline") or "",
                      "headline": f.get("headline") or a["headline"]})
        except Exception as ex:
            a["body"] = "[read failed: %s]" % str(ex)[:120]

    # See run_job: page furniture is marked before labelling, not after, so a
    # dateline never costs a classifier call or reaches the corpus.
    stories, furniture = fragments.split(arts)
    if furniture:
        print("%s %s: %d blocks marked as page furniture"
              % (jid, label, len(furniture)))

    set_job(jid, stage="%s: labelling %d articles" % (label, len(stories)))
    try:
        for a, r in zip(stories, classify.classify(stories)):
            a.update({"category": r["category"],
                      "raw_category": r["raw_category"], "tags": r["tags"]})
    except Exception as ex:
        # Unlabelled is recoverable -- /api/corpus/reclassify fills it in
        # later without re-reading the page. Losing the transcription is not,
        # so a labelling failure must never abandon the page.
        print("classify failed on %s of %s: %s" % (label, jid, ex))

    # The job's own rows, so the Result tab can show what this run produced.
    # Without this an edition job filed everything into MongoDB correctly and
    # then displayed an empty Result screen, because that view reads SQLite --
    # the work was done and looked like it had not been.
    #
    # Appended rather than replacing, since a page job clears its rows first
    # and an edition writes one page at a time; deleting here would leave only
    # the last page of a twenty-page edition.
    jobs_store().add_rows(jid, [
        {"type": a["type"], "headline": a.get("headline", ""),
         "body": a.get("body", ""), "byline": a.get("byline", ""),
         "dateline": a.get("dateline", ""), "category": a.get("category", ""),
         "bbox": json.dumps(a.get("bbox") or []), "crop": a.get("crop"),
         "n_elements": a.get("n_elements", 0),
         "has_photo": int(bool(a.get("has_photo")))} for a in arts],
        page_ref=pno)

    ingest_quietly(arts, job_id=jid, paper=spec.get("paper", ""),
                   edition=spec.get("edition", ""), date=spec.get("date", ""),
                   default_page=pno,
                   page_images={pno: {"path": page, "width": W, "height": H}})
    # Stories, not blocks. The job's n_articles is what the Result and History
    # screens report, and counting furniture there is what made a nine-story
    # front page claim twenty-three articles.
    return len(stories)


def run_ingest_job(jid, spec):
    """
    Every page of one edition: fetch, segment, read, label, file.

    Two properties matter more than speed here, because this runs unattended.

    RESUMABLE   A page already in the corpus is skipped. Reading a page costs
                about a minute of GPU time, so a run that died at page 19 must
                not start again at page 1, and a schedule that fires twice
                must not pay for the same edition twice.

    PARTIAL     One page failing does not fail the edition. A torn scan, a
                404, a model timeout on one crop -- each costs that page and
                nothing else. Twenty-three good pages are worth having, and
                the failures are counted so nobody has to read a log to find
                out the run was incomplete.
    """
    t0 = dt.datetime.now()
    d = job_dir(jid)
    try:
        set_job(jid, status="running", stage="resolving edition")
        total_pages = issue_page_count(spec["host"], spec["title_id"],
                                       spec["date"])
        if not total_pages:
            raise RuntimeError("edition reports no pages")

        # Three ways to say which pages, in order of specificity. A range is
        # what somebody asking for "the city pages" means; max_pages is the
        # older "first N" and still what the nightly round uses; neither means
        # the whole edition.
        first = max(1, int(spec.get("from_page") or 1))
        last = int(spec.get("to_page") or 0) or total_pages
        last = min(last, total_pages)
        if spec.get("max_pages"):
            last = min(last, first + int(spec["max_pages"]) - 1)
        if first > last:
            raise RuntimeError(
                "page %d is past the end of a %d-page edition"
                % (first, total_pages))

        n_pages = last - first + 1
        set_job(jid, n_pages=n_pages, pages_done=0, pages_failed=0,
                pages_skipped=0)

        c = corpus()
        done = failed = skipped = articles = 0
        errors = []
        for pno in range(first, last + 1):
            if c and not spec.get("force") and \
                    c.has_page(spec.get("paper", ""), spec.get("date", ""), pno):
                skipped += 1
                set_job(jid, pages_skipped=skipped,
                        stage="page %d of %d-%d: already held" % (pno, first, last))
                continue
            try:
                articles += ingest_one_page(jid, d, spec, pno, n_pages)
                done += 1
            except Exception as ex:
                failed += 1
                errors.append("p%d: %s" % (pno, str(ex)[:90]))
                print("edition %s page %d failed: %s" % (jid, pno, ex))
            set_job(jid, pages_done=done, pages_failed=failed,
                    n_articles=articles)

        # A run where every page failed is a failure, however many pages it
        # politely skipped past. Reporting it as "done" is how a broken
        # overnight schedule goes unnoticed for a week.
        if failed and not done and not skipped:
            raise RuntimeError("every page failed: %s" % "; ".join(errors[:3]))

        note = ""
        if failed:
            note = "%d of %d pages failed: %s" % (
                failed, n_pages, "; ".join(errors[:3]))
        set_job(jid, status="done", stage="complete", error="", note=note,
                n_articles=articles,
                elapsed=(dt.datetime.now() - t0).total_seconds())
    except Exception as ex:
        traceback.print_exc()
        set_job(jid, status="error", stage="failed", error=str(ex)[:400],
                elapsed=(dt.datetime.now() - t0).total_seconds())


# ------------------------------------------------------------ topic search

def summit_pages_to_disk(host, edition_id, date, max_pages, d):
    """Download Summit page images. Used when the publisher has not tagged stories."""
    sm = Summit(host)
    ddmmyyyy = "%s/%s/%s" % (date[8:10], date[5:7], date[0:4])
    pages = sm.pages(edition_id, ddmmyyyy)
    if not pages:
        raise RuntimeError("no edition published on %s" % date)
    out = []
    for pg in pages[:max_pages or 2]:
        # "HighResolution" is a misnomer -- it is the ~900px medium render.
        # XXHighResolution is the real full-size page (2322x3900 on Sakshi),
        # and body text at 900px is unreadable to any OCR.
        url = (pg.get("XXHighResolution") or pg.get("XHighResolution")
               or pg.get("HighResolution") or pg.get("LowResolution"))
        if not url:
            continue
        dest = os.path.join(d, "p%02d.jpg" % int(pg.get("PageNo") or len(out) + 1))
        if os.path.exists(dest) or acq_download(url, dest):
            out.append((int(pg.get("PageNo") or len(out) + 1), dest))
    if not out:
        raise RuntimeError("no page images available for %s" % date)
    return out


def collect_summit(host, edition_id, date, max_pages):
    """
    Whole edition as text, straight from the publisher -- when it exists.

    Coverage is per publisher AND per date, so it must be checked rather than
    assumed. Measured 13 Aug 2026: Namasthe Telangana had tagged stories for
    11 Aug but none for 12-13 Aug, and Sakshi returned none on any date tried
    despite serving the identical API for editions and pages. Callers must
    handle an empty list by falling back to the image pipeline.
    """
    sm = Summit(host)
    ddmmyyyy = "%s/%s/%s" % (date[8:10], date[5:7], date[0:4])
    pages = sm.pages(edition_id, ddmmyyyy)
    if not pages:
        raise RuntimeError("no edition published on %s" % date)
    out = []
    for pg in pages[:max_pages or len(pages)]:
        for row in sm.stories_on_page(pg.get("PageId")):
            body = (row.get("Content") or "").strip()
            if len(body) < 30:
                continue
            out.append({"seq": len(out) + 1, "type": "article",
                        "headline": (row.get("Summary") or body)[:120],
                        "body": body, "byline": "", "dateline": "",
                        "page_ref": int(pg.get("PageNo") or 0),
                        "story_id": row.get("storyid"),
                        "crop": None, "n_elements": 0, "has_photo": False,
                        "text_source": "publisher"})
    return out


def issue_page_count(host, title_id, date) -> int:
    """
    How many pages this issue has, whichever platform serves it.

    Split out because it was the only platform-specific step left in the
    image collector -- `fetch_page` already dispatches. Leaving it Readwhere-
    only is what made a Hindustan topic search fail with "no issue published",
    which was doubly misleading: the issue existed, and the message named the
    date rather than the real cause.
    """
    plat = platform_of(host)
    if plat == "livehindustan":
        ed = LiveHindustan(host).edition(title_id, date)
        if not ed["pages"]:
            raise RuntimeError(
                "%s has no pages for %s -- that edition has not been "
                "published yet" % (ed.get("name") or title_id, date or "today"))
        return ed["total_pages"]
    if plat == "summit":
        # The same bug the docstring above describes, repeated. Summit titles
        # fell through to the Readwhere branch, which asks an API that knows
        # nothing about them and truthfully answers "no issue" -- so Sakshi
        # reported no edition published on eight consecutive days while
        # publishing normally, and the Daily Reader showed it as waiting.
        #
        # Summit dates its issues dd/mm/yyyy, which is why this cannot simply
        # share the Readwhere path.
        sm = Summit(host)
        pages = sm.pages(title_id, "%s/%s/%s" % (date[8:10], date[5:7], date[0:4]))
        if not pages:
            raise RuntimeError("no issue published on %s" % date)
        return len(pages)
    rw = Readwhere(host)
    issue = rw.issue_for_date(title_id, date)
    if not issue:
        raise RuntimeError("no issue published on %s" % date)
    return rw.issue_config(issue).get("numPages", 0) or 1


def collect_images(jid, host, title_id, date, max_pages, d):
    """
    Articles from page images: fetch, find boundaries, read each headline.

    Platform-agnostic. Both `fetch_page` and `issue_page_count` dispatch on the
    host, so Readwhere and Hindustan share this loop.

    Bodies are deliberately NOT read here. Screening runs on headlines, and
    only the survivors get transcribed -- reading every article on every page
    would cost hours for a result that discards most of it.
    """
    total = issue_page_count(host, title_id, date)
    n = min(max_pages or total, total)

    out, missed = [], []
    for pno in range(1, n + 1):
        set_job(jid, stage="reading page %d/%d" % (pno, n))
        page = os.path.join(d, "p%02d.jpg" % pno)
        try:
            if not os.path.exists(page):
                fetch_page(host, title_id, date, pno, page)
            regions = local_backend.detect_regions(page, pno, debug=False,
                                                   lang=lang_of(host))
        except Exception as ex:
            # Skipped pages have to reach the UI. A gated title silently
            # yielding one page out of fourteen looks identical to a thin
            # edition, and the search that follows is then quietly answering
            # over a fraction of the paper.
            print("page %s failed: %s" % (pno, ex))
            missed.append(pno)
            continue
        for r in regions:
            if r.get("type") != "article":
                continue
            seq = len(out) + 1
            crop = "p%02d_a%02d.jpg" % (pno, seq)
            try:
                crop_region(page, r["bbox_px"], os.path.join(d, crop))
            except Exception:
                continue
            out.append({"seq": seq, "type": "article",
                        "headline": r.get("headline_visible", ""), "body": "",
                        "byline": "", "dateline": "", "page_ref": pno,
                        "crop": crop, "bbox": r["bbox_px"],
                        "region_boxes": r.get("region_boxes") or [],
                        "n_elements": len(r.get("elements") or []),
                        "has_photo": bool(r.get("has_photo")),
                        "text_source": "vlm"})
    if missed:
        # Why a page was skipped depends on the platform, and guessing wrong
        # is worse than saying less: the paywall explanation is true of the
        # Tribune titles and simply false of Hindustan.
        why = (" This title serves page 1 free and gates the rest."
               if platform_of(host) == "readwhere" else "")
        set_job(jid, note=("read %d of %d page(s); %d unavailable (%s).%s"
                           % (n - len(missed), n, len(missed),
                              ", ".join("p%d" % p for p in missed), why)))
    return out


def resolve_mode(requested, kw):
    """
    Which relevance engine to use, and why.

    "keywords" is only offered for a resolved beat: without one there is no
    vocabulary beyond the user's own words, and lexical matching on those alone
    is worse than the model, not cheaper-but-comparable.

    "hybrid" degrades to "keywords" rather than failing when the model server is
    unreachable. A lexical answer with a warning beats a stack trace, and the
    caller is told which engine actually ran.
    """
    if requested == "keywords":
        if not kw["expanded"]:
            return "hybrid", ("no keyword vocabulary for %r; used the model"
                              % kw["query"][:60])
        return "keywords", ""
    try:
        h = vlm.health()
        if h.get("available"):
            return "hybrid", ""
        detail = h.get("error") or "model server unavailable"
    except Exception as ex:
        detail = str(ex)[:120]
    if kw["expanded"]:
        return "keywords", "model unavailable (%s); ranked by keywords only" % detail[:90]
    return "hybrid", ""


def run_archive_job(jid, spec):
    """
    The same question, asked of what has already been read.

    A topic job normally fetches pages and reads them, which costs minutes per
    page and can only ever answer about one date. Everything already extracted
    is sitting in the corpus with its keywords and beat scores precomputed, so
    the same question over that costs milliseconds and can span a month.

    Runs inline rather than as a background task: there is nothing to wait for,
    and a progress panel that appears and vanishes reads as a glitch.
    """
    t0 = dt.datetime.now()
    c = corpus()
    if not c:
        set_job(jid, status="error", stage="failed",
                error="corpus unavailable: %s" % (_corpus_err or "mongodb down"))
        return

    date_from, date_to = epaper_query.resolve_timeframe(
        spec.get("timeframe") or "today",
        date_from=spec.get("date_from") or spec.get("date"),
        date_to=spec.get("date_to") or spec.get("date"))
    days = epaper_query.parse_weekdays(spec.get("weekdays"))

    set_job(jid, status="running", stage="searching the archive")
    res = c.search(
        spec["topic"], paper=spec.get("paper") or None,
        date_from=date_from, date_to=date_to,
        category=spec.get("category") or None,
        exclude=spec.get("exclude"), proximity=spec.get("proximity"),
        weekdays=days, limit=200,
        min_score=float(spec.get("min_score") or 0.0))

    rows = [{"type": "article", "headline": r.get("headline", ""),
             "body": r.get("body", ""), "byline": r.get("byline", ""),
             "dateline": r.get("dateline", ""),
             "category": r.get("category", ""),
             "bbox": json.dumps(r.get("bbox") or []), "crop": r.get("crop"),
             "n_elements": 0, "has_photo": 0,
             "topic_score": r.get("score"),
             "topic_why": r.get("why") or "",
             "topic_summary": "",
             "page_ref": r.get("page_number") or 0,
             "keywords": json.dumps(r.get("hits") or [], ensure_ascii=False)}
            for r in res["results"]]
    jobs_store().reset_rows(jid, rows)

    span = ("%s to %s" % (date_from, date_to)) if date_from and date_to \
        else "the whole archive"
    set_job(jid, status="done", stage="complete", error="",
            n_articles=len(rows), searched=res.get("scanned") or 0,
            screened=len(rows), prefiltered=0,
            beat=res.get("beat") or "", mode="archive",
            elapsed=(dt.datetime.now() - t0).total_seconds(),
            note="searched %s in %.0f ms; no pages fetched"
                 % (span, res.get("took_ms") or 0))


def run_topic_job(jid, spec):
    t0 = dt.datetime.now()
    d = job_dir(jid)
    topic = spec["topic"]
    try:
        # Nothing to fetch, nothing to read: answer from the corpus and stop.
        if spec.get("scope") == "archive":
            run_archive_job(jid, spec)
            return
        set_job(jid, status="running", stage="collecting articles")
        arts = []
        if spec["platform"] == "summit":
            arts = collect_summit(spec["host"], spec["title_id"], spec["date"],
                                  spec.get("max_pages") or 0)
            if not arts:
                # Publisher text missing for this paper/date -- fall back to the
                # image pipeline rather than reporting nothing found.
                set_job(jid, stage="no publisher text; reading page images")
                for pno, page in summit_pages_to_disk(
                        spec["host"], spec["title_id"], spec["date"],
                        spec.get("max_pages") or 2, d):
                    set_job(jid, stage="reading page %d" % pno)
                    try:
                        regions = local_backend.detect_regions(
                            page, pno, debug=False, lang=lang_of(spec["host"]))
                    except Exception as ex:
                        print("page %s failed: %s" % (pno, ex))
                        continue
                    for r in regions:
                        if r.get("type") != "article":
                            continue
                        seq = len(arts) + 1
                        crop = "p%02d_a%02d.jpg" % (pno, seq)
                        try:
                            crop_region(page, r["bbox_px"], os.path.join(d, crop))
                        except Exception:
                            continue
                        arts.append({"seq": seq, "type": "article",
                                     "headline": r.get("headline_visible", ""),
                                     "body": "", "byline": "", "dateline": "",
                                     "page_ref": pno, "crop": crop,
                                     "bbox": r["bbox_px"],
                                     "n_elements": len(r.get("elements") or []),
                                     "has_photo": bool(r.get("has_photo")),
                                     "text_source": "vlm"})
        else:
            arts = collect_images(jid, spec["host"], spec["title_id"],
                                  spec["date"], spec.get("max_pages") or 2, d)
        if not arts:
            raise RuntimeError("no articles found to search")

        min_score = float(spec.get("min_score") or 0.5)
        kw = keywords.expand(topic)
        mode, warning = resolve_mode(spec.get("mode") or "hybrid", kw)
        set_job(jid, searched=len(arts), mode=mode, beat=kw["beat"] or "")

        # Free stage, always run: it supplies the stored keyword evidence even
        # when the model decides relevance.
        set_job(jid, stage="matching keywords over %d articles" % len(arts))
        pf = keywords.prefilter(arts, kw)
        set_job(jid, prefiltered=pf["dropped"])

        # Bodies read during assessment are worth keeping even when the article
        # is judged irrelevant: the transcription was paid for either way, and
        # the corpus is not a record of this query's verdict.
        enriched = {}

        if mode == "keywords":
            matches = keywords.rank(arts, kw, min_score)
            set_job(jid, screened=len(arts))
        else:
            cand = [arts[i] for i in pf["keep"]]
            set_job(jid, stage="screening %d of %d articles" % (len(cand), len(arts)))
            # Already prefiltered above; screening it a second time would only
            # recompute the same scores.
            kept = topics.screen(cand, topic, spec=kw, prefilter=False)
            keep = [pf["keep"][k] for k in kept]
            set_job(jid, screened=len(keep))

            matches = []
            for n, i in enumerate(keep, 1):
                set_job(jid, stage="assessing %d/%d" % (n, len(keep)))
                a = dict(arts[i])
                # Image-sourced candidates have no body yet: read it now, only
                # for the few that survived screening.
                if not a.get("body") and a.get("crop"):
                    try:
                        f = local_backend.transcribe(os.path.join(d, a["crop"]))
                        a["body"] = f.get("body") or ""
                        a["headline"] = f.get("headline") or a["headline"]
                        a["byline"] = f.get("byline") or ""
                        a["dateline"] = f.get("dateline") or ""
                    except Exception:
                        pass
                enriched[i] = a
                v = topics.assess(a, topic)
                # Geography is applied to the model's score too, not just the
                # lexical one -- otherwise the beat's own region gate has no
                # effect on the results this mode actually returns.
                adj = keywords.adjust(v["score"], a, kw)
                if v["relevant"] and adj["score"] >= min_score:
                    why = v["why"]
                    if adj["reason"]:
                        why = "%s [scored down: %s]" % (why, adj["reason"])
                    a.update({"topic_score": adj["score"], "topic_why": why,
                              "topic_summary": v["summary"],
                              "keywords": adj["hits"]})
                    matches.append(a)
            matches.sort(key=lambda m: -m["topic_score"])

        # The same refinements the archive path pushes into MongoDB, applied
        # here in Python. One definition in epaper/query.py, two callers, so
        # "exclude traffic" cannot come to mean different things depending on
        # whether the articles were fetched or recalled.
        #
        # Applied last, after screening and assessment, rather than as an early
        # filter. It costs a little more model time, but `enriched` keeps the
        # bodies that were read -- so an article excluded from THIS answer is
        # still transcribed and still reaches the corpus, where a query without
        # the exclusion will find it.
        drop = epaper_query.parse_terms(spec.get("exclude"))
        window = epaper_query.window_for(spec.get("proximity"))
        if drop or window is not None:
            before = len(matches)
            kept = []
            for m in matches:
                text = "%s %s" % (m.get("headline") or "", m.get("body") or "")
                hit = epaper_query.excluded_by(text, drop)
                if hit:
                    continue
                if not epaper_query.within(text, topic, window):
                    continue
                kept.append(m)
            matches = kept
            if before != len(matches):
                print("%s: %d of %d ruled out by exclude/proximity"
                      % (jid, before - len(matches), before))

        jobs_store().reset_rows(jid, [
            {"type": "article", "headline": a.get("headline", ""),
             "body": a.get("body", ""), "byline": a.get("byline", ""),
             "dateline": a.get("dateline", ""),
             "category": kw["label"] if kw["beat"] else "",
             "bbox": json.dumps(a.get("bbox") or []), "crop": a.get("crop"),
             "n_elements": a.get("n_elements", 0),
             "has_photo": int(bool(a.get("has_photo"))),
             "topic_score": a.get("topic_score"),
             "topic_why": a.get("topic_why"),
             "topic_summary": a.get("topic_summary"),
             "page_ref": a.get("page_ref"),
             "keywords": json.dumps(a.get("keywords") or [],
                                    ensure_ascii=False)} for a in matches])

        # Index everything collected, preferring the transcribed version where
        # one exists. Done in one pass: ingesting arts and matches separately
        # would write the same story twice, because transcription can rewrite
        # the headline and the dedup key is derived from it.
        ingest_quietly([enriched.get(i, a) for i, a in enumerate(arts)],
                       job_id=jid, paper=spec.get("paper", ""),
                       edition=spec.get("edition", ""), date=spec.get("date", ""))

        set_job(jid, status="done", stage="complete", error="", note=warning,
                n_articles=len(matches),
                elapsed=(dt.datetime.now() - t0).total_seconds())
    except Exception as ex:
        traceback.print_exc()
        set_job(jid, status="error", stage="failed", error=str(ex)[:400],
                elapsed=(dt.datetime.now() - t0).total_seconds())


# ---------------------------------------------------------------- API

@app.get("/api/health")
def health():
    h = local_backend.health()
    return {"ready": h.get("ready"), "gpu": h.get("gpu"),
            "layout": h.get("weights_present"), "mode": h.get("mode"),
            "vlm": h.get("vlm", {}).get("model"),
            "vlm_ok": h.get("vlm", {}).get("available"),
            "detail": h.get("vlm", {}).get("error") or h.get("layout_error") or ""}


@app.get("/api/papers")
def papers():
    return CATALOGUE


@app.get("/api/beats")
def beats(all: bool = False, q: str = "", samples: bool = False):
    """
    The beats a user can pick from: one per state force, plus any added since.

    `q` filters by name or state so a 36-entry dropdown stays usable, and
    `all=true` includes disabled beats for the admin view.
    """
    rows = keywords.catalogue(enabled_only=not all, samples=samples)
    needle = (q or "").strip().lower()
    if needle:
        rows = [r for r in rows
                if needle in r["label"].lower()
                or needle in (r.get("state") or "").lower()
                or needle in (r.get("state_hi") or "")
                or needle == (r.get("state_code") or "").lower()]
    return rows


@app.get("/api/beats/{beat_id}")
def beat_detail(beat_id: str):
    """One beat in full: every term, grouped by tier, plus its geography."""
    b = keywords.BEATS.get(beat_id)
    if not b:
        raise HTTPException(404, "no beat %r" % beat_id)
    return {
        "id": b["id"], "label": b.get("label"), "kind": b.get("kind"),
        "state": b.get("state"), "state_hi": b.get("state_hi"),
        "state_code": b.get("state_code"), "topic": b.get("topic"),
        "enabled": b.get("enabled", True), "builtin": bool(b.get("builtin")),
        "pack": b.get("pack") or "",
        "aliases": b.get("aliases") or [],
        "tiers": [{"tier": beats_mod.tier_of(w), "weight": w,
                   "n": len(terms), "terms": list(terms)}
                  for w, terms in (b.get("terms") or [])],
        "acronyms": [{"weight": w, "terms": list(t)}
                     for w, t in (b.get("acronyms") or [])],
        "region": b.get("region") or [],
        "n_region_out": len(b.get("region_out") or []),
        "source": "mongodb" if _beats_source == "mongodb" else "built-in",
    }


def beat_store():
    """The editable beat store, or a 503 explaining why there isn't one."""
    s = beats_mod.store()
    if not s:
        raise HTTPException(
            503, "beats are read-only: %s. The built-in vocabulary is still "
                 "serving searches." % (beats_mod.last_error() or "mongodb down"))
    return s


def _reload_beats():
    """Re-install from MongoDB after an edit, so the change takes immediately."""
    global _beats_source
    s = beats_mod.store()
    if s:
        n = s.load_into_keywords()
        _beats_source = "mongodb"
        return n
    return len(keywords.BEATS)


@app.post("/api/beats/seed")
def beats_seed(force: bool = False):
    """
    Write the built-in 36 state forces into MongoDB.

    Safe to re-run: an existing beat is left alone unless `force=true`, because
    a redeploy silently reverting someone's added terms is worse than a stale
    seed.
    """
    counts = beat_store().seed(force=force)
    return {**counts, "active": _reload_beats()}


@app.post("/api/beats")
def beat_create(payload: dict):
    """Create or replace a beat outright. Body is the beat document."""
    if not payload.get("id"):
        raise HTTPException(400, "a beat needs an id")
    doc = beat_store().upsert(payload)
    return {"beat": doc, "active": _reload_beats()}


@app.post("/api/beats/{beat_id}/terms")
def beat_add_terms(beat_id: str, payload: dict):
    """
    Add vocabulary. The endpoint for "we missed a word".

    `scope: "beat"` adds it to this force only. `scope: "pack"` adds it to the
    shared policing vocabulary, which means all 36 at once -- that is the right
    scope for an ordinary policing word, and the wrong one for a place name.
    """
    terms = [t for t in (payload.get("terms") or []) if str(t).strip()]
    if not terms:
        raise HTTPException(400, "no terms given")
    tier = (payload.get("tier") or "related").lower()
    if tier not in beats_mod.TIERS:
        raise HTTPException(400, "tier must be one of %s"
                            % ", ".join(beats_mod.TIERS))
    s = beat_store()
    try:
        if (payload.get("scope") or "beat") == "pack":
            beat = keywords.BEATS.get(beat_id) or {}
            pack = beat.get("pack") or "policing"
            doc = s.add_pack_terms(pack, tier, terms)
            scope = "pack:%s" % pack
        else:
            doc = s.add_terms(beat_id, tier, terms)
            scope = "beat:%s" % beat_id
    except KeyError:
        raise HTTPException(404, "no beat %r" % beat_id)
    return {"scope": scope, "tier": tier, "added": terms,
            "active": _reload_beats(),
            "n_terms": sum(len(g.get("terms") or [])
                           for g in (doc.get("terms") or []))}


@app.post("/api/beats/{beat_id}/places")
def beat_add_places(beat_id: str, payload: dict):
    """
    Extend a force's geography.

    One write covers everything: because `region_out` is derived at load time,
    a district added here starts counting as elsewhere for the other 35 beats
    on the same reload.
    """
    places = [p for p in (payload.get("places") or []) if str(p).strip()]
    if not places:
        raise HTTPException(400, "no places given")
    try:
        doc = beat_store().add_places(beat_id, places)
    except KeyError:
        raise HTTPException(404, "no beat %r" % beat_id)
    return {"id": beat_id, "region": doc.get("region"),
            "added": places, "active": _reload_beats(),
            "note": "run /api/corpus/rescore to apply this to stored articles"}


@app.patch("/api/beats/{beat_id}")
def beat_patch(beat_id: str, payload: dict):
    """Enable or disable a beat, or change its label, topic or aliases."""
    s = beat_store()
    if not s.get(beat_id):
        raise HTTPException(404, "no beat %r" % beat_id)
    if "enabled" in payload:
        s.set_enabled(beat_id, bool(payload["enabled"]))
    fields = {k: v for k, v in payload.items()
              if k in ("label", "topic", "aliases", "region", "state",
                       "state_hi", "state_code", "kind")}
    if fields:
        s.upsert({**s.get(beat_id), **fields})
    return {"beat": s.get(beat_id), "active": _reload_beats()}


@app.delete("/api/beats/{beat_id}")
def beat_delete(beat_id: str):
    """Delete a custom beat. A built-in one is disabled instead."""
    try:
        r = beat_store().delete(beat_id)
    except KeyError:
        raise HTTPException(404, "no beat %r" % beat_id)
    return {**r, "active": _reload_beats()}


@app.get("/api/expand")
def expand(topic: str):
    """
    What a topic will actually be searched for. Lets the operator see the
    vocabulary before spending minutes of GPU on it.
    """
    kw = keywords.expand(topic)
    return {"query": kw["query"], "beat": kw["beat"], "label": kw["label"],
            "expanded": kw["expanded"], "n_terms": len(kw["terms"]),
            "terms": kw["terms"]}


@app.get("/api/editions")
def editions(host: str):
    if host in _cache:
        return _cache[host]
    try:
        plat = platform_of(host)
        if plat == "summit":
            t = [{"id": e["id"], "name": "%s - %s" % (e["group"], e["name"])}
                 for e in Summit(host).editions() if e.get("id")]
        elif plat == "livehindustan":
            # The city slug IS the id. Names come back in Devanagari with the
            # state alongside, so the label carries both -- "आगरा (उत्तर प्रदेश)"
            # is what makes the coverage grouping in the UI legible.
            t = [{"id": c["id"],
                  "name": "%s (%s)" % (c["name"], c["region"]) if c["region"]
                          else c["name"]}
                 for c in LiveHindustan(host).cities()]
        else:
            t = Readwhere(host).titles()
    except Exception as ex:
        raise HTTPException(502, "could not list editions: %s" % ex)
    _cache[host] = t
    return t


@app.get("/api/dates")
def dates(host: str, title_id: str):
    # Neither Summit nor Hindustan publishes a date index; both answer per
    # date instead. Offer the recent calendar and let the fetch report an
    # unpublished day -- Hindustan signals that as totalPages 0, not an error.
    if platform_of(host) in ("summit", "livehindustan"):
        today = dt.date.today()
        return [{"date": (today - dt.timedelta(days=i)).isoformat(), "issue_id": None}
                for i in range(0, 30)]
    try:
        rows = Readwhere(host).publish_dates(title_id, days_back=60)
    except Exception as ex:
        raise HTTPException(502, "could not list dates: %s" % ex)
    out = [{"date": r["published"][:10], "issue_id": r["id"]} for r in rows]
    return list(reversed(out))[:60]


def _create(spec, paper, edition, date, page_no, with_text):
    jid = uuid.uuid4().hex[:12]
    jobs_store().create(
        jid, created=dt.datetime.now().isoformat(timespec="seconds"),
        source=spec["source"], paper=paper, edition=edition, date=date,
        page_no=page_no, status="queued", stage="queued",
        with_text=int(bool(with_text)))
    return jid


@app.post("/api/jobs/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...),
                 with_text: str = Form("0")):
    jid = _create({"source": "upload"}, file.filename or "upload", "", "", 1,
                  with_text == "1")
    tmp = os.path.join(job_dir(jid), "src_" + (file.filename or "page"))
    with open(tmp, "wb") as f:
        f.write(await file.read())
    background.add_task(run_job, jid, {"source": "upload", "tmp": tmp,
                                       "paper": file.filename or "upload",
                                       "page_no": 1,
                                       "with_text": with_text == "1"})
    return {"job_id": jid}


@app.post("/api/jobs/fetch")
def fetch(background: BackgroundTasks, payload: dict):
    for k in ("host", "title_id", "date"):
        if not payload.get(k):
            raise HTTPException(400, "missing %s" % k)
    paper = next((p["name"] for p in CATALOGUE if p["host"] == payload["host"]),
                 payload["host"])
    page_no = int(payload.get("page_no") or 1)
    jid = _create({"source": "catalogue"}, paper, payload.get("edition", ""),
                  payload["date"], page_no, bool(payload.get("with_text")))
    background.add_task(run_job, jid, {"source": "catalogue", **payload,
                                       "paper": paper, "page_no": page_no,
                                       "with_text": bool(payload.get("with_text"))})
    return {"job_id": jid}


@app.post("/api/jobs/ingest")
def ingest_edition(background: BackgroundTasks, payload: dict):
    """
    Collect a whole edition: every page, read, labelled and filed.

    The overnight path. Long-running by nature -- roughly a minute of GPU time
    per page -- so it returns a job id immediately and reports progress through
    /api/jobs/{id}. Re-running it for a date already collected is cheap and
    safe: held pages are skipped unless `force` is set.
    """
    for k in ("host", "title_id", "date"):
        if not payload.get(k):
            raise HTTPException(400, "missing %s" % k)
    paper = payload.get("paper") or next(
        (p["name"] for p in CATALOGUE if p["host"] == payload["host"]),
        payload["host"])
    jid = _create({"source": "ingest"}, paper, payload.get("edition", ""),
                  payload["date"], 0, True)
    set_job(jid, kind="edition")
    background.add_task(run_ingest_job, jid, {
        "source": "ingest", "host": payload["host"],
        "title_id": payload["title_id"], "date": payload["date"],
        "paper": paper, "edition": payload.get("edition", ""),
        "max_pages": int(payload.get("max_pages") or 0),
        "from_page": int(payload.get("from_page") or 0),
        "to_page": int(payload.get("to_page") or 0),
        "force": bool(payload.get("force")),
    })
    return {"job_id": jid}


# ----------------------------------------------------------------- sources

def source_store():
    s = src_mod.store()
    if not s:
        raise HTTPException(503, "sources unavailable: %s" % src_mod.last_error())
    return s


@app.get("/api/daily")
def daily_reader(date: str = None):
    """
    One day's collection: what was asked for, what arrived, what is missing.

    Deliberately answers about the READER'S day rather than about jobs. The
    History tab already lists runs; this lists papers, and a paper that was
    due and produced nothing has to appear here as an absence -- a Daily
    Reader that only lists what succeeded cannot tell "Sakshi published
    nothing" from "Sakshi was never registered".
    """
    day = date or dt.date.today().isoformat()
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s"
                            % (_corpus_err or "mongodb down"))

    collected = {(r["paper"], r["edition"]): r for r in c.by_paper(day)}

    s = src_mod.store()
    registered = s.listing() if s else []
    rows = []
    for src in registered:
        if not src.get("enabled"):
            continue
        key = (src.get("paper") or "", src.get("edition") or "")
        got = collected.get(key)
        rows.append({
            "source_id": src["id"], "paper": key[0], "edition": key[1],
            "lang": src.get("lang") or "", "registered": True,
            "n": (got or {}).get("n", 0),
            "labelled": (got or {}).get("labelled", 0),
            "pages": (got or {}).get("pages", []),
            "categories": (got or {}).get("categories", []),
            # Three states, not two, and "failed" is about THIS day. A paper
            # that failed yesterday and has not been tried today is waiting,
            # not failed -- showing it red taught the reader to ignore red,
            # which is the one thing the colour is for. Before ~06:00 nearly
            # every paper is legitimately waiting.
            "status": ("collected" if got else
                       "failed" if (src.get("last_error")
                                    and src.get("last_error_date") == day)
                       else "waiting"),
            "last_date": src.get("last_date") or "",
            "last_error": (src.get("last_error") or "")[:160],
            "fail_count": src.get("fail_count") or 0})

    # Deliberately NOT padded with whatever else was extracted that day. A
    # one-off run from the New tab is somebody's experiment, and listing it
    # here made the Daily Reader answer a different question than it asks:
    # this page is "did the papers I subscribed to arrive", and three Tribune
    # editions nobody subscribed to sitting above two Telugu papers that did
    # not is the wrong answer to it. Those articles are still in Browse.
    rows.sort(key=lambda r: (r["status"] != "collected", -r["n"], r["paper"]))
    return {"date": day,
            "total": sum(r["n"] for r in rows),
            "collected": sum(1 for r in rows if r["status"] == "collected"),
            "waiting": sum(1 for r in rows if r["status"] == "waiting"),
            "failed": sum(1 for r in rows if r["status"] == "failed"),
            "sources": rows,
            "schedule": _schedule_state()}


def _schedule_state() -> dict:
    """
    Whether the nightly collection is actually registered with Windows.

    Read from Task Scheduler rather than from a setting of our own, because a
    setting would say "on" while the task had been deleted, disabled, or never
    created on this machine -- and the whole point of this panel is to answer
    "will it run tonight without me".
    """
    try:
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", SCHEDULED_TASK, "/FO", "LIST"],
            capture_output=True, text=True, timeout=10)
    except Exception as ex:
        return {"supported": False, "registered": False, "detail": str(ex)[:120]}
    if out.returncode != 0:
        return {"supported": True, "registered": False, "detail": ""}
    info = {}
    for line in (out.stdout or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip().lower()] = v.strip()
    return {"supported": True, "registered": True,
            "status": info.get("status", ""),
            "next_run": info.get("next run time", ""),
            "last_run": info.get("last run time", ""),
            "last_result": info.get("last result", "")}


@app.post("/api/daily/collect")
def daily_collect(background: BackgroundTasks, date: str = None):
    """
    Run tonight's collection now, for every enabled source not yet done.

    The same work the scheduled task does, on demand -- for the first run
    after registering a paper, and for the morning after a night the machine
    was asleep.
    """
    s = source_store()
    day = date or dt.date.today().isoformat()
    due = s.due(day)
    if not due:
        return {"started": False, "date": day, "due": 0,
                "detail": "every enabled paper already has %s" % day}
    background.add_task(_collect_due, day)
    return {"started": True, "date": day, "due": len(due),
            "papers": [r.get("paper") for r in due]}


_collecting = threading.Lock()


def _collect_due(day: str):
    """
    One edition job per due source, in series. Failures are per-paper.

    Serialised across requests. Pressing Collect now twice used to start two
    rounds, and because a source is only marked after its job finishes, both
    read the same due list and fetched the same edition -- six identical
    Himachal Tribune jobs in the history, all doing the same work.
    """
    if not _collecting.acquire(blocking=False):
        print("daily: a collection is already running; ignoring this one")
        return
    try:
        s = source_store()
        for src in s.due(day):
            jid = _create({"source": "ingest"}, src.get("paper") or src["host"],
                          src.get("edition", ""), day, 0, True)
            set_job(jid, kind="edition")
            try:
                run_ingest_job(jid, {
                    "source": "ingest", "host": src["host"],
                    "title_id": src["title_id"], "date": day,
                    "paper": src.get("paper") or src["host"],
                    "edition": src.get("edition", ""),
                    "max_pages": int(src.get("max_pages") or 0),
                    "force": False})
            except Exception as ex:
                # Recorded against the title, not raised: one paper that has
                # not published by 02:00 must not stop the rest of the round.
                print("daily: %s raised: %s" % (src.get("paper"), ex))
                s.mark_failed(src["id"], str(ex)[:200], day)
                continue

            # The job's OWN status, not the absence of an exception.
            # run_ingest_job catches its failures and records them as
            # status="error" rather than raising, so a try/except around it
            # never fires -- and this marked Sakshi collected on a day it
            # published nothing. `due()` then skipped it for the rest of the
            # day, so the retry that should have happened at the next run
            # never did. scripts/daily.py has always checked the status; this
            # is the same rule, in the one place that had drifted from it.
            j = jobs_store().get(jid) or {}
            if j.get("status") == "done":
                s.mark_done(src["id"], day, pages=j.get("pages_done") or 0,
                            articles=j.get("n_articles") or 0)
            else:
                s.mark_failed(src["id"],
                              j.get("error") or "job did not finish", day)
    finally:
        _collecting.release()


@app.post("/api/daily/schedule")
def daily_schedule(payload: dict = None):
    """
    Register (or remove) the nightly run with Windows Task Scheduler.

    Deliberately not a thread inside this process. An in-process timer stops
    the moment the app does -- a closed terminal, a reboot, a crash -- and
    gives no way to answer "did last night run" except by reading logs. A
    scheduled task survives all of those, runs whether or not the app is open,
    and records its own exit code, which is why scripts/daily.py exits 1 when
    any paper failed.
    """
    payload = payload or {}
    if not payload.get("enabled", True):
        out = subprocess.run(["schtasks", "/Delete", "/TN", SCHEDULED_TASK, "/F"],
                             capture_output=True, text=True, timeout=20)
        if out.returncode != 0 and "cannot find" not in (out.stderr or "").lower():
            raise HTTPException(500, (out.stderr or "could not remove")[:200])
        return {"registered": False}

    at = str(payload.get("at") or "05:30")
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at):
        raise HTTPException(400, "at must be HH:MM in 24-hour time")

    py = sys.executable
    root = os.path.dirname(HERE)
    script = os.path.join(root, "scripts", "daily.py")
    if not os.path.exists(script):
        raise HTTPException(500, "scripts/daily.py not found at %s" % script)

    # Registered through the ScheduledTasks cmdlets rather than schtasks.exe,
    # because three of schtasks' defaults quietly stop this from being a daily
    # collection at all on a laptop:
    #
    #   StartWhenAvailable=False        the machine is asleep at 05:30, the run
    #                                   is skipped outright and never caught up
    #   DisallowStartIfOnBatteries=True unplugged, it does not start
    #   StopIfGoingOnBatteries=True     unplug mid-run and it is killed
    #
    # Together those mean "collect every day" would have meant "collect on
    # days the machine happened to be awake and on mains at exactly 05:30".
    # schtasks.exe cannot set any of them; these cmdlets can.
    #
    # WakeToRun is deliberately NOT set. Waking somebody's machine at dawn to
    # fetch a newspaper is not a reasonable thing to do uninvited, and
    # StartWhenAvailable already covers the case -- the run happens shortly
    # after the machine next wakes.
    ps = (
        "$ErrorActionPreference='Stop';"
        "$a = New-ScheduledTaskAction -Execute '{py}'"
        " -Argument '\"{script}\"' -WorkingDirectory '{root}';"
        "$t = New-ScheduledTaskTrigger -Daily -At {at};"
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable"
        " -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries"
        " -ExecutionTimeLimit (New-TimeSpan -Hours 6)"
        " -MultipleInstances IgnoreNew;"
        "Register-ScheduledTask -TaskName '{task}' -Action $a -Trigger $t"
        " -Settings $s -Description 'Clipping Desk: collect registered"
        " newspapers for today' -Force | Out-Null;"
        "'ok'"
    ).format(py=py.replace("'", "''"), script=script.replace("'", "''"),
             root=root.replace("'", "''"), at=at, task=SCHEDULED_TASK)

    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                          "-Command", ps],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise HTTPException(500, (out.stderr or out.stdout or "failed")[:250])
    return dict({"registered": True, "at": at,
                 "command": '"%s" "%s"' % (py, script)},
                **_schedule_state())


@app.get("/api/sources")
def sources_list(enabled_only: bool = False, due_for: str = None):
    """
    The titles registered for collection.

    `due_for=<date>` answers the scheduler's question -- enabled titles with
    nothing collected for that date yet.
    """
    s = src_mod.store()
    if not s:
        return {"available": False, "error": src_mod.last_error(), "sources": []}
    try:
        rows = s.due(due_for) if due_for else s.listing(enabled_only=enabled_only)
        return {"available": True, "sources": rows}
    except Exception as ex:
        return {"available": False, "error": str(ex)[:200], "sources": []}


@app.post("/api/sources")
def source_add(payload: dict):
    """Register a title for daily collection, or update its settings."""
    if not payload.get("host") or not payload.get("title_id"):
        raise HTTPException(400, "host and title_id are required")
    paper = payload.get("paper") or next(
        (p["name"] for p in CATALOGUE if p["host"] == payload["host"]),
        payload["host"])
    try:
        return source_store().add(
            host=payload["host"], title_id=payload["title_id"], paper=paper,
            edition=payload.get("edition", ""),
            lang=payload.get("lang") or lang_of(payload["host"]),
            max_pages=int(payload.get("max_pages") or 0),
            enabled=bool(payload.get("enabled", True)))
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.patch("/api/sources/{sid:path}")
def source_patch(sid: str, payload: dict):
    """
    Change a title's settings without forgetting its collection history.

    Both fields are optional and applied independently, so a request that
    only changes the page limit does not have to restate whether the title is
    enabled -- and cannot accidentally re-enable one somebody switched off.
    """
    if "enabled" not in payload and "max_pages" not in payload:
        raise HTTPException(
            400, "nothing to change; send enabled and/or max_pages")
    try:
        s = source_store()
        row = None
        if "enabled" in payload:
            row = s.set_enabled(sid, bool(payload["enabled"]))
        if "max_pages" in payload:
            n = int(payload["max_pages"] or 0)
            if n < 0 or n > 200:
                raise HTTPException(400, "max_pages must be 0-200 (0 = all)")
            row = s.set_max_pages(sid, n)
        return row
    except KeyError:
        raise HTTPException(404, "no such source: %s" % sid)


@app.delete("/api/sources/{sid:path}")
def source_delete(sid: str):
    try:
        return source_store().remove(sid)
    except KeyError:
        raise HTTPException(404, "no such source: %s" % sid)


@app.post("/api/jobs/topic")
def topic_job(background: BackgroundTasks, payload: dict):
    archive = payload.get("scope") == "archive"
    # An archive search needs no publisher and no page, because it fetches
    # nothing -- requiring a host and a date to search dates already stored
    # would be asking for the answer as a precondition of the question.
    required = ("topic",) if archive else ("host", "title_id", "date", "topic")
    for k in required:
        if not payload.get(k):
            raise HTTPException(400, "missing %s" % k)
    host = payload.get("host") or ""
    paper = next((p["name"] for p in CATALOGUE if p["host"] == host), host)
    plat = platform_of(host) if host else "archive"
    mode = payload.get("mode") or "hybrid"
    if mode not in ("hybrid", "keywords"):
        raise HTTPException(400, "mode must be 'hybrid' or 'keywords'")
    if payload.get("proximity") and payload["proximity"] not in epaper_query.WINDOWS:
        raise HTTPException(400, "proximity must be one of %s"
                            % ", ".join(epaper_query.WINDOWS))
    if payload.get("timeframe") and payload["timeframe"] not in epaper_query.TIMEFRAMES:
        raise HTTPException(400, "timeframe must be one of %s"
                            % ", ".join(epaper_query.TIMEFRAMES))
    kw = keywords.expand(payload["topic"])
    jid = uuid.uuid4().hex[:12]
    jobs_store().create(
        jid, created=dt.datetime.now().isoformat(timespec="seconds"),
        source=plat, paper=paper, edition=payload.get("edition", ""),
        date=payload.get("date") or "", page_no=0, status="queued",
        stage="queued", kind="topic", topic=payload["topic"],
        mode="archive" if archive else mode, beat=kw["beat"] or "")
    # `paper` is resolved from the catalogue here, not carried in the payload,
    # so it has to be passed explicitly -- the corpus derives an article's
    # identity from it, and an empty paper silently breaks deduplication.
    job_spec = {**payload, "platform": plat, "mode": mode,
                "paper": paper if host else "",
                "title_id": str(payload.get("title_id") or "")}
    if archive:
        # Inline, not backgrounded. It is one indexed read, so returning a
        # queued job the caller then has to poll would add latency to hide
        # latency that is not there.
        run_topic_job(jid, job_spec)
    else:
        background.add_task(run_topic_job, jid, job_spec)
    return {"job_id": jid, "platform": plat,
            "mode": "archive" if archive else mode,
            "beat": kw["beat"], "n_terms": len(kw["terms"])}


@app.get("/api/search")
def search(q: str, paper: str = None, date_from: str = None,
           date_to: str = None, beat: str = None, category: str = None,
           subcategory: str = None, tag: str = None, limit: int = 50,
           min_score: float = 0.0, exclude: str = None,
           proximity: str = None, weekdays: str = None,
           timeframe: str = None):
    """
    Search everything ever extracted. No page fetch, no model, milliseconds.

    This is the query path the topic job is not: a topic job re-reads a page to
    answer one question, while this answers any number of questions about every
    page already read.

    `timeframe` is a convenience over date_from/date_to -- "week", "month" and
    the rest resolve here rather than in the browser, so a request made at
    23:59 and served at 00:01 still means the day the reader was looking at.
    Explicit dates win when both are given.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s" % (_corpus_err or "mongodb down"))
    if timeframe and not (date_from or date_to):
        date_from, date_to = epaper_query.resolve_timeframe(timeframe)
    try:
        return c.search(q, paper=paper, date_from=date_from, date_to=date_to,
                        beat=beat, category=category, subcategory=subcategory,
                        tag=tag, limit=min(limit, 200), min_score=min_score,
                        exclude=exclude, proximity=proximity,
                        weekdays=(weekdays or "").split(",") if weekdays else None)
    except Exception as ex:
        raise HTTPException(500, "search failed: %s" % str(ex)[:200])


# ------------------------------------------------------------------ browse
#
# The reader who has a date rather than a question. Three endpoints, none of
# which scores, expands or calls a model -- each is one indexed read, so the
# whole journey costs milliseconds however large the archive grows.

@app.get("/api/corpus/dates")
def corpus_dates(paper: str = None, limit: int = 120):
    """
    Days the archive holds, newest first, with article counts.

    Deliberately not /api/dates, which already exists and answers a different
    question -- what the PUBLISHER has available to fetch. This one is what we
    have already read. Conflating them would make an empty archive look like a
    paper that never published.
    """
    c = corpus()
    if not c:
        return []
    try:
        return c.dates(paper=paper, limit=min(limit, 400))
    except Exception as ex:
        raise HTTPException(500, "dates failed: %s" % str(ex)[:200])


@app.get("/api/facets")
def facets(date: str = None, paper: str = None, date_from: str = None,
           date_to: str = None, category: str = None, limit: int = 40,
           timeframe: str = None):
    """
    Which categories appeared on a day, and how many articles each holds.

    Pass `category` to scope the returned tags to that category -- those tags
    are its sub-categories.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s"
                            % (_corpus_err or "mongodb down"))
    # Resolved here, not in the browser: a page opened before midnight and
    # used after it would otherwise keep asking for the wrong "today".
    if timeframe and not (date or date_from or date_to):
        date_from, date_to = epaper_query.resolve_timeframe(timeframe)
    try:
        return c.facets(date=date, paper=paper, date_from=date_from,
                        date_to=date_to, category=category,
                        limit=min(limit, 200))
    except Exception as ex:
        raise HTTPException(500, "facets failed: %s" % str(ex)[:200])


@app.get("/api/categories/tree")
def categories_tree(date: str = None, paper: str = None,
                    date_from: str = None, date_to: str = None,
                    limit: int = 30, sub_limit: int = 8, min_sub: int = 2):
    """
    Categories with their sub-categories, in one call.

    Nothing declares this hierarchy. A sub-category is a tag that occurs
    inside a category -- "murder" within "crime" -- so the tree is a
    co-occurrence count over what the classifier already produced, and it
    updates itself every night without anybody maintaining a taxonomy.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s"
                            % (_corpus_err or "mongodb down"))
    try:
        return c.tree(date=date, paper=paper, date_from=date_from,
                      date_to=date_to, limit=min(limit, 100),
                      sub_limit=min(sub_limit, 30), min_sub=min_sub)
    except Exception as ex:
        raise HTTPException(500, "tree failed: %s" % str(ex)[:200])


@app.post("/api/categories")
def category_create(payload: dict):
    """
    Add a topic by hand, before the classifier has ever produced it.

    The list is normally grown by the classifier, which is the point of
    free-form labelling. This is for the cases that design does not cover: a
    subject you know is coming, a heading you want articles filed under today,
    or a target to merge two sloppy entries into.

    Created at zero articles, so it appears in the picker and in the merge
    target list without pretending anything is filed under it yet.
    """
    label = (payload.get("label") or payload.get("id") or "").strip()
    if not label:
        raise HTTPException(400, "send {\"label\": \"...\"}")
    filed = catdata.normalise(label)
    if not filed:
        raise HTTPException(
            400, "%r is not usable as a topic -- one to three words, not a "
                 "sentence" % label[:60])
    s = cat_mod.store()
    if not s:
        raise HTTPException(503, "categories unavailable: %s" % cat_mod.last_error())
    if s.get(filed):
        return s.get(filed)
    s.create(filed, display=payload.get("display") or "")
    return s.get(filed)


@app.patch("/api/article/{uid}")
def article_patch(uid: str, payload: dict):
    """
    Correct one article's topic or tags.

    The counterpart to the review queue: that screen fixes the topic LIST,
    this fixes one ARTICLE. Both are needed -- merging "roadworks" into
    "infrastructure" does nothing for a story the classifier filed under
    "politics" by mistake.

    A correction made here is marked as human and survives every later bulk
    relabel.
    """
    if "category" not in payload and "tags" not in payload:
        raise HTTPException(
            400, "send {\"category\": \"...\"} and/or {\"tags\": [...]}")
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s"
                            % (_corpus_err or "mongodb down"))
    cat = payload.get("category")
    if isinstance(cat, str) and cat.strip() and not catdata.normalise(cat):
        raise HTTPException(
            400, "%r is not usable as a topic -- one to three words, not a "
                 "sentence" % cat[:60])
    try:
        return c.set_topic(uid, category=cat, tags=payload.get("tags"))
    except KeyError:
        raise HTTPException(404, "no such article: %s" % uid)


@app.get("/api/categories/unplaced")
def categories_unplaced(min_count: int = 1):
    """Subcategories with no heading yet, most-used first."""
    s = cat_mod.store()
    if not s:
        raise HTTPException(503, "categories unavailable: %s" % cat_mod.last_error())
    return {"unplaced": s.unplaced(min_count=min_count)}


@app.post("/api/categories/assign-parents")
def categories_assign_parents(min_count: int = 1, limit: int = 200):
    """
    Place every unplaced subcategory under a heading, using the model.

    Costs one call per twenty-five LABELS, not per article: an archive with
    200 distinct subcategories is eight calls however many articles carry
    them. Obvious ones never reach the model at all -- catdata.SEED_PARENTS
    answers them for free.
    """
    s = cat_mod.store()
    if not s:
        raise HTTPException(503, "categories unavailable: %s" % cat_mod.last_error())
    rows = s.unplaced(min_count=min_count)[:limit]
    if not rows:
        return {"asked": 0, "placed": 0, "unplaced": 0, "assignments": {}}
    h = vlm.health()
    if not h.get("available"):
        raise HTTPException(503, "model server unavailable: %s"
                            % (h.get("error") or "unknown")[:200])
    got = classify.assign_parents([r["id"] for r in rows])
    placed = 0
    for lab, parent in got.items():
        if not parent:
            continue
        try:
            s.set_parent(lab, parent)
            placed += 1
        except Exception:
            pass
    return {"asked": len(rows), "placed": placed,
            "unplaced": len(s.unplaced(min_count=min_count)),
            "assignments": {k: v for k, v in got.items() if v}}


@app.patch("/api/categories/{cat_id:path}/parent")
def category_set_parent(cat_id: str, payload: dict):
    """Place one subcategory under a heading by hand, or unplace it."""
    s = cat_mod.store()
    if not s:
        raise HTTPException(503, "categories unavailable: %s" % cat_mod.last_error())
    try:
        return s.set_parent(cat_id, payload.get("parent") or "")
    except KeyError:
        raise HTTPException(404, "no such category: %s" % cat_id)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


# The two-level tree is served from /api/categories/tree above, derived by
# co-occurrence over what the classifier already produced. The registry's own
# parent links are not a second source of truth for it -- they exist so that
# labels predating the second level can be placed under a heading (see
# scripts/build_tree.py), and are surfaced as a queue below.




@app.get("/api/categories/headings")
def category_headings():
    """
    The fixed top level, with what each heading means and what files under it.

    Controlled on purpose -- see epaper/catdata.py. Returned in full rather
    than as bare ids because this is what the Categories tab shows a reader
    who wants to know the scheme before correcting anything, and a list of
    twenty one-word ids does not tell anybody where an article belongs.

    `desk` marks the five police headings. `children` is only the SEEDED
    subcategories -- the ones decided in advance. What the classifier has
    actually invented is a different question, answered by /api/categories.
    """
    return {
        "headings": [{
            "id": p,
            "label": catdata.display(p),
            "note": catdata.PARENT_NOTES.get(p, ""),
            "desk": p in catdata.POLICE_PARENTS,
            "children": catdata.children_of(p),
        } for p in catdata.PARENTS],
        "police_desk": catdata.POLICE_PARENTS,
        "unclassified": catdata.UNKNOWN,
        # Stated because it is the question people ask about a bilingual
        # archive, and because the answer is not obvious from the list: the
        # headings are English regardless of the paper's language, since a
        # corpus labelled in each article's own language splits every subject
        # in two and neither half is complete.
        "language_note": (
            "Headings are English whatever language the paper is in — a "
            "Telugu, Hindi or English article about a robbery all file under "
            "crime. The original wording is kept in tags and in the article "
            "text, so searching in the paper's own language still works."),
    }


@app.post("/api/categories/merge")
def categories_merge(payload: dict):
    """
    File everything under one topic as another, now and in future.

    The review queue's only destructive-looking action, and it is reversible:
    the merged entry stays, pointing at its target, so a label arriving
    tomorrow still lands in the right place and the decision remains visible.
    """
    src, dst = (payload.get("src") or "").strip(), (payload.get("dst") or "").strip()
    if not src or not dst:
        raise HTTPException(400, "send {\"src\": ..., \"dst\": ...}")
    s = cat_mod.store()
    if not s:
        raise HTTPException(503, "categories unavailable: %s" % cat_mod.last_error())
    c = corpus()
    try:
        return s.merge(src, dst, articles=c.articles if c else None)
    except KeyError:
        raise HTTPException(404, "no such topic: %s" % src)
    except ValueError as ex:
        raise HTTPException(400, str(ex))


@app.patch("/api/categories/{cat_id:path}")
def category_patch(cat_id: str, payload: dict):
    """Hide a topic from the picker, or rename how it is displayed."""
    s = cat_mod.store()
    if not s:
        raise HTTPException(503, "categories unavailable: %s" % cat_mod.last_error())
    try:
        if "hidden" in payload:
            return s.set_hidden(cat_id, bool(payload["hidden"]))
        if payload.get("label"):
            return s.rename(cat_id, payload["label"])
        raise HTTPException(400, "send {\"hidden\": bool} or {\"label\": str}")
    except KeyError:
        raise HTTPException(404, "no such topic: %s" % cat_id)


@app.post("/api/categories/rebuild")
def categories_rebuild():
    """
    Recount the topic list from the articles themselves.

    Stored counts are incremented as articles arrive, so they drift when
    articles are deleted, re-ingested or relabelled. The articles are the
    truth; this makes the list agree with them.
    """
    s = cat_mod.store()
    c = corpus()
    if not s or not c:
        raise HTTPException(503, "categories or corpus unavailable")
    return s.rebuild(c.articles)


@app.get("/api/browse")
def browse(date: str = None, category: str = None, subcategory: str = None,
           paper: str = None, tag: str = None, date_from: str = None,
           date_to: str = None, page: int = 1, per_page: int = 50,
           timeframe: str = None):
    """
    A day's articles, or one topic within it, in the order they were printed.

    At least one narrowing filter is required. Without one this is "every
    article ever extracted, page 1", which is not a question anybody asked and
    gets slower every night the archive runs.
    """
    if timeframe and not (date or date_from or date_to):
        date_from, date_to = epaper_query.resolve_timeframe(timeframe)
    if not any([date, category, subcategory, paper, tag, date_from, date_to]):
        raise HTTPException(
            400, "give at least one of: date, category, paper, tag, "
                 "date_from, date_to, timeframe")
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s"
                            % (_corpus_err or "mongodb down"))
    try:
        return c.browse(date=date, category=category, subcategory=subcategory,
                        paper=paper, tag=tag, date_from=date_from,
                        date_to=date_to, page=page, per_page=per_page)
    except Exception as ex:
        raise HTTPException(500, "browse failed: %s" % str(ex)[:200])


@app.get("/api/categories")
def categories_list(min_count: int = 1, include_hidden: bool = False):
    """
    The topic list as it stands, most-used first.

    Reads the registry rather than aggregating the articles, so it carries the
    display labels and merge decisions that the raw facet counts do not.
    """
    s = cat_mod.store()
    if not s:
        return {"available": False, "error": cat_mod.last_error(),
                "categories": []}
    try:
        return {"available": True,
                "categories": s.listing(min_count=min_count,
                                        include_hidden=include_hidden)}
    except Exception as ex:
        return {"available": False, "error": str(ex)[:200], "categories": []}


@app.get("/api/article/{uid}")
def get_article(uid: str):
    """One article with its page and issue resolved, plus typed region boxes."""
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable")
    a = c.article(uid)
    if not a:
        raise HTTPException(404, "no such article")
    return a


@app.get("/api/search/history")
def search_history(limit: int = 30):
    """What has been searched for. The best guide to which beat to build next."""
    c = corpus()
    if not c:
        return []
    try:
        return c.history_recent(limit)
    except Exception:
        return []


@app.get("/api/corpus")
def corpus_stats():
    """
    How much has been indexed, by paper and by beat.

    Reports the connection too (host and database, password redacted). A
    running server holds whatever configuration it started with, and there is
    no way to tell from the outside whether it picked up an edited `.env`
    without restarting -- so it says so rather than leaving it to be inferred
    from surprising results.
    """
    conn = epaper_config.describe()
    c = corpus()
    if not c:
        return {"available": False, "error": _corpus_err or "mongodb down",
                "connection": conn, "articles": 0, "by_paper": [],
                "by_beat": [], "by_category": []}
    try:
        return {"available": True, "connection": conn, **c.stats()}
    except Exception as ex:
        return {"available": False, "error": str(ex)[:200],
                "connection": conn, "articles": 0, "by_paper": [],
                "by_beat": [], "by_category": []}


@app.post("/api/corpus/rescore")
def corpus_rescore():
    """
    Refresh stored beat scores after a vocabulary change.

    Search reads precomputed scores, so a new term or weight in keywords.py
    has no effect on results until this runs. No model, no page re-read.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s" % (_corpus_err or "mongodb down"))
    return c.rescore()


@app.post("/api/corpus/reclassify")
def corpus_reclassify(only_unlabelled: bool = True, limit: int = 0,
                      date: str = None, paper: str = None):
    """
    Assign topics to articles already extracted.

    Runs synchronously and can take minutes -- roughly 3.6 s per article on a
    3B model. Pass `limit` to sample before committing to a whole archive.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s"
                            % (_corpus_err or "mongodb down"))
    h = vlm.health()
    if not h.get("available"):
        # Checked up front rather than discovered per article. Without this
        # the run "succeeds" having marked every article as a failure, which
        # then has to be undone.
        raise HTTPException(503, "model server unavailable: %s"
                            % (h.get("error") or "unknown")[:200])
    try:
        return c.reclassify(only_unlabelled=only_unlabelled, limit=limit,
                            date=date, paper=paper)
    except Exception as ex:
        raise HTTPException(500, "reclassify failed: %s" % str(ex)[:200])


def backfill_rows(job: dict, arts: list) -> list:
    """
    A job's rows, with `category` kept only where it means a subject.

    That field does not mean one thing, and which thing it means is decided by
    the job's kind:

        page, edition   the classifier's subject -- "politics", "crime"
        topic           the BEAT LABEL -- "Delhi Police", for the results view

    Passing a topic job's value through files a police force as a subject and
    rebuilds the exact collision the primary_beat/category split removed.

    Discarding ALL of them -- which is what this did -- is the other half of
    that mistake and the more expensive half. It stripped the classifier's work
    off every page and edition job on the way in, so a backfill left 103 of 104
    articles with an empty category while their own job rows still held
    "politics", "crime" and "business". Browse then had nothing to group by and
    looked broken, because it was.

    A separate function, and not an inline loop, so the rule can be tested
    without running a backfill against the live corpus.
    """
    if (job.get("kind") or "page") != "topic":
        return arts
    out = []
    for a in arts:
        a = dict(a)
        a.pop("category", None)
        out.append(a)
    return out


@app.post("/api/corpus/backfill")
def corpus_backfill():
    """
    Index articles from previous runs into the searchable corpus.

    Extraction is the expensive part and it has already been paid for; there is
    no reason for earlier jobs to be invisible to search.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s" % (_corpus_err or "mongodb down"))
    total = {"added": 0, "updated": 0, "skipped": 0, "jobs": 0}
    # Oldest first, explicitly. An article dedupes on a uid keyed to page
    # geometry rather than text, so every extraction of one page writes to
    # the same document and the last write wins. Re-extracting a page after
    # fixing the reader therefore leaves several rows holding the same
    # story at different quality -- observed here as three jobs for
    # Tribune p1, two carrying "SCtellsSIT" and one "SC tells SIT".
    #
    # Unordered, which row landed last was whatever the database happened
    # to return, so this button could silently revert a headline to an
    # older, worse reading of it.
    s = jobs_store()
    for j, arts in s.with_articles(oldest_first=True):
        if j.get("status") != "done":
            continue
        arts = backfill_rows(j, arts)
        r = c.ingest(arts, job_id=j["id"], paper=j["paper"] or "",
                     edition=j["edition"] or "", date=j["date"] or "",
                     default_page=j["page_no"] or 0)
        for k in ("added", "updated", "skipped"):
            total[k] += r[k]
        total["jobs"] += 1
    return total


@app.get("/api/jobs")
def list_jobs(limit: int = 60, kind: str = None):
    """
    Recent jobs, newest first.

    `kind` accepts a comma-separated list, because the New tab wants the runs
    a person started by hand -- "page,upload" -- and those are two kinds, not
    one. Without it the tab would show last night's edition collection mixed
    in with the reader's own extractions, which are separate activities that
    only happen to share a table.
    """
    kinds = [k.strip() for k in (kind or "").split(",") if k.strip()]
    return jobs_store().listing(limit, kinds=kinds or None)


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    s = jobs_store()
    j = s.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return {"job": j, "articles": s.rows(jid)}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    jobs_store().delete(jid)
    shutil.rmtree(os.path.join(JOBS, jid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/media/{jid}/{name}")
def media(jid: str, name: str):
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = os.path.join(JOBS, jid, name)
    if not os.path.exists(p):
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="image/jpeg")


@app.exception_handler(Exception)
def on_error(request, exc):
    return JSONResponse({"error": str(exc)[:300]}, status_code=500)


# ---------------------------------------------------------------- frontend
#
# The React/Vite SPA in web/frontend is prebuilt to web/frontend/dist. Mounted
# last, after every /api route above, so those routes keep matching first and
# this mount only catches the rest: "/" -> index.html, "/assets/*" -> the JS
# and CSS bundle, "/favicon.svg" -> the icon. No server-side routing needed --
# the whole app is one page that switches views client-side.

FRONTEND_DIST = os.path.join(HERE, "frontend", "dist")

if os.path.isdir(FRONTEND_DIST):
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
else:
    @app.get("/", response_class=HTMLResponse)
    def frontend_missing():
        return HTMLResponse(
            "<pre>frontend not built.\n\n"
            "  cd web/frontend\n"
            "  npm install\n"
            "  npm run build\n\n"
            "then restart this server.</pre>",
            status_code=503)


def _load_beats():
    """
    Install the stored vocabulary, seeding an empty database with the built-in
    36 state forces.

    Never fatal. If MongoDB is unreachable the process keeps the beats compiled
    into the code, which is a complete working vocabulary -- searches carry on,
    only editing is unavailable. The line it prints says which of the two is
    actually in play, because "why did my new term not take effect" is
    otherwise unanswerable from outside.
    """
    global _beats_source
    r = beats_mod.bootstrap()
    _beats_source = r.get("source", "built-in")
    seeded = r.get("seeded") or {}
    print("beats: %d active from %s%s%s"
          % (r["loaded"], _beats_source,
             (" (seeded %d)" % seeded["added"]) if seeded.get("added") else "",
             (" -- %s" % r["error"]) if r.get("error") else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reload", action="store_true",
                    help="restart on a Python source change (development)")
    a = ap.parse_args()
    init_db()
    import uvicorn
    print("UI  ->  http://%s:%d" % (a.host, a.port))
    if not a.reload:
        uvicorn.run(app, host=a.host, port=a.port, log_level="info")
        return
    # Reload needs an import string rather than the object: uvicorn re-imports
    # the module in a fresh child process on every change, and cannot re-import
    # an object it was handed. app_dir puts web/ on sys.path so "app:app"
    # resolves without web/ having to be a package.
    #
    # Only these two directories are watched. web/data holds the job SQLite and
    # every page image a run writes, so watching the tree wholesale would have
    # each extraction restart the server mid-job. uvicorn matches *.py by
    # default, which keeps the images out on its own -- naming the directories
    # keeps the database out too.
    uvicorn.run("app:app", host=a.host, port=a.port, log_level="info",
                reload=True, app_dir=HERE,
                reload_dirs=[HERE, os.path.join(ROOT, "epaper")])


if __name__ == "__main__":
    main()
