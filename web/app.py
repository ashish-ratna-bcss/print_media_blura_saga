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
import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import traceback
import uuid

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, ImageDraw

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from epaper import local_backend, topics, vlm    # noqa: E402
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
app = FastAPI(title="E-Paper Extraction")
_lock = threading.Lock()

# Two kinds of source, and the difference decides how good topic search can be.
#   summit    - the publisher serves its own article text. No OCR, exact words,
#               whole edition in seconds. Topic search here is fast and precise.
#   readwhere - page images only. Every article has to be found and read first,
#               so topic search costs minutes per page.
CATALOGUE = [
    {"name": "Sakshi", "host": "epaper.sakshi.com", "lang": "te", "platform": "summit"},
    {"name": "Namasthe Telangana", "host": "epaper.ntnews.com", "lang": "te", "platform": "summit"},
    {"name": "The Tribune", "host": "epaper.tribuneindia.com", "lang": "en", "platform": "readwhere"},
    {"name": "Dainik Tribune", "host": "epaper.dainiktribuneonline.com", "lang": "hi", "platform": "readwhere"},
    {"name": "Jansatta", "host": "epaper.jansatta.com", "lang": "hi", "platform": "readwhere"},
    {"name": "Financial Express", "host": "epaper.financialexpress.com", "lang": "en", "platform": "readwhere"},
    {"name": "The Statesman", "host": "epaper.thestatesman.com", "lang": "en", "platform": "readwhere"},
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
            screened INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS articles(
            job_id TEXT, seq INTEGER, type TEXT, headline TEXT, body TEXT,
            byline TEXT, dateline TEXT, category TEXT, bbox TEXT,
            crop TEXT, n_elements INTEGER, has_photo INTEGER,
            topic_score REAL, topic_why TEXT, topic_summary TEXT,
            page_ref INTEGER)""")
        # tolerate databases created before topic search existed
        for tbl, col, typ in (("jobs", "kind", "TEXT"), ("jobs", "topic", "TEXT"),
                              ("jobs", "searched", "INTEGER"), ("jobs", "screened", "INTEGER"),
                              ("articles", "topic_score", "REAL"),
                              ("articles", "topic_why", "TEXT"),
                              ("articles", "topic_summary", "TEXT"),
                              ("articles", "page_ref", "INTEGER")):
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
    """Pull one page from Readwhere and stitch its tile pyramid."""
    rw = Readwhere(host)
    issue = rw.issue_for_date(title_id, date)
    if not issue:
        raise RuntimeError("no issue published for %s on %s" % (title_id, date))
    cfg = rw.issue_config(issue)
    n = cfg.get("numPages", 0)
    meta = rw.page_meta(issue, page_no, page_no)
    pm = meta.get(str(page_no)) or meta.get(page_no) or (
        list(meta.values())[0] if meta else None)
    if not pm:
        raise RuntimeError("page %s unavailable for issue %s" % (page_no, issue))
    level = rw.best_level(pm)
    if not stitch_tiles(rw.tiles(level), level["width"], level["height"], dest):
        raise RuntimeError("page tiles could not be assembled")
    return {"issue_id": issue, "num_pages": n, "title_name": cfg.get("titleName", "")}


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
        json.dump(regions, open(os.path.join(d, "regions.json"), "w"),
                  ensure_ascii=False, indent=1, default=str)

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


def collect_readwhere(jid, host, title_id, date, max_pages, d):
    """
    Articles from page images: fetch, find boundaries, read each headline.

    Bodies are deliberately NOT read here. Screening runs on headlines, and
    only the survivors get transcribed -- reading every article on every page
    would cost hours for a result that discards most of it.
    """
    rw = Readwhere(host)
    issue = rw.issue_for_date(title_id, date)
    if not issue:
        raise RuntimeError("no issue published on %s" % date)
    total = rw.issue_config(issue).get("numPages", 0) or 1
    n = min(max_pages or total, total)

    out = []
    for pno in range(1, n + 1):
        set_job(jid, stage="reading page %d/%d" % (pno, n))
        page = os.path.join(d, "p%02d.jpg" % pno)
        try:
            if not os.path.exists(page):
                fetch_page(host, title_id, date, pno, page)
            regions = local_backend.detect_regions(page, pno, debug=False)
        except Exception as ex:
            print("page %s failed: %s" % (pno, ex))
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
                        "n_elements": len(r.get("elements") or []),
                        "has_photo": bool(r.get("has_photo")),
                        "text_source": "vlm"})
    return out


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
            arts = collect_readwhere(jid, spec["host"], spec["title_id"],
                                     spec["date"], spec.get("max_pages") or 2, d)
        if not arts:
            raise RuntimeError("no articles found to search")

        set_job(jid, searched=len(arts), stage="screening %d articles" % len(arts))
        keep = topics.screen(arts, topic)
        set_job(jid, screened=len(keep))

        matches = []
        for n, i in enumerate(keep, 1):
            set_job(jid, stage="assessing %d/%d" % (n, len(keep)))
            a = dict(arts[i])
            # Image-sourced candidates have no body yet: read it now, only for
            # the few that survived screening.
            if not a.get("body") and a.get("crop"):
                try:
                    f = local_backend.transcribe(os.path.join(d, a["crop"]))
                    a["body"] = f.get("body") or ""
                    a["headline"] = f.get("headline") or a["headline"]
                    a["byline"] = f.get("byline") or ""
                    a["dateline"] = f.get("dateline") or ""
                except Exception:
                    pass
            v = topics.assess(a, topic)
            if v["relevant"] and v["score"] >= float(spec.get("min_score") or 0.5):
                a.update({"topic_score": v["score"], "topic_why": v["why"],
                          "topic_summary": v["summary"]})
                matches.append(a)
        matches.sort(key=lambda m: -m["topic_score"])

        with _lock, db() as c:
            c.execute("DELETE FROM articles WHERE job_id=?", (jid,))
            for k, a in enumerate(matches, 1):
                c.execute("""INSERT INTO articles(job_id,seq,type,headline,body,
                    byline,dateline,category,bbox,crop,n_elements,has_photo,
                    topic_score,topic_why,topic_summary,page_ref)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (jid, k, "article", a.get("headline", ""),
                           a.get("body", ""), a.get("byline", ""),
                           a.get("dateline", ""), "",
                           json.dumps(a.get("bbox") or []), a.get("crop"),
                           a.get("n_elements", 0), int(bool(a.get("has_photo"))),
                           a.get("topic_score"), a.get("topic_why"),
                           a.get("topic_summary"), a.get("page_ref")))

        set_job(jid, status="done", stage="complete", error="",
                n_articles=len(matches),
                elapsed=(dt.datetime.now() - t0).total_seconds())
    except Exception as ex:
        traceback.print_exc()
        set_job(jid, status="error", stage="failed", error=str(ex)[:400],
                elapsed=(dt.datetime.now() - t0).total_seconds())


# ---------------------------------------------------------------- API

@app.get("/", response_class=HTMLResponse)
def index():
    return open(os.path.join(HERE, "ui.html"), encoding="utf-8").read()


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


@app.get("/api/editions")
def editions(host: str):
    if host in _cache:
        return _cache[host]
    try:
        if platform_of(host) == "summit":
            t = [{"id": e["id"], "name": "%s - %s" % (e["group"], e["name"])}
                 for e in Summit(host).editions() if e.get("id")]
        else:
            t = Readwhere(host).titles()
    except Exception as ex:
        raise HTTPException(502, "could not list editions: %s" % ex)
    _cache[host] = t
    return t


@app.get("/api/dates")
def dates(host: str, title_id: str):
    # Summit has no publish-date index; it answers per date instead. Offer the
    # recent calendar and let the fetch report an unpublished day.
    if platform_of(host) == "summit":
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
    jid = uuid.uuid4().hex[:12]
    with _lock, db() as c:
        c.execute("""INSERT INTO jobs(id,created,source,paper,edition,date,
            page_no,status,stage,kind,topic) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                  (jid, dt.datetime.now().isoformat(timespec="seconds"),
                   plat, paper, payload.get("edition", ""), payload["date"],
                   0, "queued", "queued", "topic", payload["topic"]))
    background.add_task(run_topic_job, jid, {**payload, "platform": plat,
                                             "title_id": str(payload["title_id"])})
    return {"job_id": jid, "platform": plat}


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
