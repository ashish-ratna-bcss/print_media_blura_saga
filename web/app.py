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
import shutil
import sqlite3
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
DB = os.path.join(DATA, "jobs.db")
os.makedirs(JOBS, exist_ok=True)

Image.MAX_IMAGE_PIXELS = None


@contextlib.asynccontextmanager
async def lifespan(_app):
    """Install the stored beat vocabulary before the first request."""
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
_cache = {}


# ---------------------------------------------------------------- storage

def db():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, created TEXT, source TEXT, paper TEXT,
            edition TEXT, date TEXT, page_no INTEGER, status TEXT, stage TEXT,
            error TEXT, n_articles INTEGER DEFAULT 0, n_ads INTEGER DEFAULT 0,
            coverage REAL DEFAULT 0, width INTEGER, height INTEGER,
            with_text INTEGER DEFAULT 0, elapsed REAL DEFAULT 0,
            kind TEXT DEFAULT 'page', topic TEXT, searched INTEGER DEFAULT 0,
            screened INTEGER DEFAULT 0, mode TEXT, beat TEXT, note TEXT,
            prefiltered INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS articles(
            job_id TEXT, seq INTEGER, type TEXT, headline TEXT, body TEXT,
            byline TEXT, dateline TEXT, category TEXT, bbox TEXT,
            crop TEXT, n_elements INTEGER, has_photo INTEGER,
            topic_score REAL, topic_why TEXT, topic_summary TEXT,
            page_ref INTEGER, keywords TEXT)""")
        # tolerate databases created before topic search / keyword search existed
        for tbl, col, typ in (("jobs", "kind", "TEXT"), ("jobs", "topic", "TEXT"),
                              ("jobs", "searched", "INTEGER"), ("jobs", "screened", "INTEGER"),
                              ("jobs", "mode", "TEXT"), ("jobs", "beat", "TEXT"),
                              ("jobs", "note", "TEXT"),
                              ("jobs", "prefiltered", "INTEGER"),
                              ("articles", "topic_score", "REAL"),
                              ("articles", "topic_why", "TEXT"),
                              ("articles", "topic_summary", "TEXT"),
                              ("articles", "page_ref", "INTEGER"),
                              ("articles", "keywords", "TEXT")):
            try:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tbl, col, typ))
            except sqlite3.OperationalError:
                pass


def set_job(jid, **kw):
    if not kw:
        return
    with _lock, db() as c:
        c.execute("UPDATE jobs SET %s WHERE id=?" % ",".join("%s=?" % k for k in kw),
                  list(kw.values()) + [jid])


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
    if platform_of(host) == "livehindustan":
        return fetch_page_lh(host, title_id, date, page_no, dest)
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
            debug=False)

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
                              "category": f.get("category") or "",
                              "headline": f.get("headline") or a["headline"]})
                except Exception as ex:
                    a["body"] = "[read failed: %s]" % str(ex)[:120]

        with _lock, db() as c:
            c.execute("DELETE FROM articles WHERE job_id=?", (jid,))
            for a in arts:
                c.execute("""INSERT INTO articles(job_id,seq,type,headline,body,
                    byline,dateline,category,bbox,crop,n_elements,has_photo)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (jid, a["seq"], a["type"], a["headline"],
                           a.get("body", ""), a.get("byline", ""),
                           a.get("dateline", ""), a.get("category", ""),
                           json.dumps(a["bbox"]), a["crop"], a["n_elements"],
                           int(a["has_photo"])))
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
    if platform_of(host) == "livehindustan":
        ed = LiveHindustan(host).edition(title_id, date)
        if not ed["pages"]:
            raise RuntimeError(
                "%s has no pages for %s -- that edition has not been "
                "published yet" % (ed.get("name") or title_id, date or "today"))
        return ed["total_pages"]
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
            regions = local_backend.detect_regions(page, pno, debug=False)
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


def run_topic_job(jid, spec):
    t0 = dt.datetime.now()
    d = job_dir(jid)
    topic = spec["topic"]
    try:
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
                        regions = local_backend.detect_regions(page, pno, debug=False)
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

        with _lock, db() as c:
            c.execute("DELETE FROM articles WHERE job_id=?", (jid,))
            for k, a in enumerate(matches, 1):
                c.execute("""INSERT INTO articles(job_id,seq,type,headline,body,
                    byline,dateline,category,bbox,crop,n_elements,has_photo,
                    topic_score,topic_why,topic_summary,page_ref,keywords)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (jid, k, "article", a.get("headline", ""),
                           a.get("body", ""), a.get("byline", ""),
                           a.get("dateline", ""), kw["label"] if kw["beat"] else "",
                           json.dumps(a.get("bbox") or []), a.get("crop"),
                           a.get("n_elements", 0), int(bool(a.get("has_photo"))),
                           a.get("topic_score"), a.get("topic_why"),
                           a.get("topic_summary"), a.get("page_ref"),
                           json.dumps(a.get("keywords") or [], ensure_ascii=False)))

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
    with _lock, db() as c:
        c.execute("""INSERT INTO jobs(id,created,source,paper,edition,date,
            page_no,status,stage,with_text) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                  (jid, dt.datetime.now().isoformat(timespec="seconds"),
                   spec["source"], paper, edition, date, page_no,
                   "queued", "queued", int(bool(with_text))))
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


@app.post("/api/jobs/topic")
def topic_job(background: BackgroundTasks, payload: dict):
    for k in ("host", "title_id", "date", "topic"):
        if not payload.get(k):
            raise HTTPException(400, "missing %s" % k)
    host = payload["host"]
    paper = next((p["name"] for p in CATALOGUE if p["host"] == host), host)
    plat = platform_of(host)
    mode = payload.get("mode") or "hybrid"
    if mode not in ("hybrid", "keywords"):
        raise HTTPException(400, "mode must be 'hybrid' or 'keywords'")
    kw = keywords.expand(payload["topic"])
    jid = uuid.uuid4().hex[:12]
    with _lock, db() as c:
        c.execute("""INSERT INTO jobs(id,created,source,paper,edition,date,
            page_no,status,stage,kind,topic,mode,beat)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (jid, dt.datetime.now().isoformat(timespec="seconds"),
                   plat, paper, payload.get("edition", ""), payload["date"],
                   0, "queued", "queued", "topic", payload["topic"], mode,
                   kw["beat"] or ""))
    # `paper` is resolved from the catalogue here, not carried in the payload,
    # so it has to be passed explicitly -- the corpus derives an article's
    # identity from it, and an empty paper silently breaks deduplication.
    background.add_task(run_topic_job, jid, {**payload, "platform": plat,
                                             "mode": mode, "paper": paper,
                                             "title_id": str(payload["title_id"])})
    return {"job_id": jid, "platform": plat, "mode": mode,
            "beat": kw["beat"], "n_terms": len(kw["terms"])}


@app.get("/api/search")
def search(q: str, paper: str = None, date_from: str = None,
           date_to: str = None, beat: str = None, limit: int = 50,
           min_score: float = 0.0):
    """
    Search everything ever extracted. No page fetch, no model, milliseconds.

    This is the query path the topic job is not: a topic job re-reads a page to
    answer one question, while this answers any number of questions about every
    page already read.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s" % (_corpus_err or "mongodb down"))
    try:
        return c.search(q, paper=paper, date_from=date_from, date_to=date_to,
                        beat=beat, limit=min(limit, 200), min_score=min_score)
    except Exception as ex:
        raise HTTPException(500, "search failed: %s" % str(ex)[:200])


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
                "connection": conn, "articles": 0, "by_paper": [], "by_beat": []}
    try:
        return {"available": True, "connection": conn, **c.stats()}
    except Exception as ex:
        return {"available": False, "error": str(ex)[:200],
                "connection": conn, "articles": 0, "by_paper": [], "by_beat": []}


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


@app.post("/api/corpus/backfill")
def corpus_backfill():
    """
    Index articles already sitting in SQLite from previous runs.

    Extraction is the expensive part and it has already been paid for; there is
    no reason for earlier jobs to be invisible to search.
    """
    c = corpus()
    if not c:
        raise HTTPException(503, "corpus unavailable: %s" % (_corpus_err or "mongodb down"))
    total = {"added": 0, "updated": 0, "skipped": 0, "jobs": 0}
    with db() as conn:
        jobs = conn.execute(
            "SELECT id,paper,edition,date,page_no FROM jobs WHERE status='done'"
        ).fetchall()
        for j in jobs:
            arts = [dict(a) for a in conn.execute(
                "SELECT * FROM articles WHERE job_id=?", (j["id"],)).fetchall()]
            if not arts:
                continue
            r = c.ingest(arts, job_id=j["id"], paper=j["paper"] or "",
                         edition=j["edition"] or "", date=j["date"] or "",
                         default_page=j["page_no"] or 0)
            for k in ("added", "updated", "skipped"):
                total[k] += r[k]
            total["jobs"] += 1
    return total


@app.get("/api/jobs")
def list_jobs(limit: int = 60):
    with db() as c:
        rows = c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?",
                         (limit,)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    with db() as c:
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j:
            raise HTTPException(404, "no such job")
        arts = c.execute("SELECT * FROM articles WHERE job_id=? ORDER BY seq",
                         (jid,)).fetchall()
    return {"job": dict(j), "articles": [dict(a) for a in arts]}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    with _lock, db() as c:
        c.execute("DELETE FROM jobs WHERE id=?", (jid,))
        c.execute("DELETE FROM articles WHERE job_id=?", (jid,))
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
    a = ap.parse_args()
    init_db()
    import uvicorn
    print("UI  ->  http://%s:%d" % (a.host, a.port))
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
