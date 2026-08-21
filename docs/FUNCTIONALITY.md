# Functionality

What the system does, stage by stage. Every claim below is anchored to code.

---

## 1. The problem it solves

A newspaper page is one image. The job is to come back with *separate articles* — each
headline attached to its own body, its own byline, its own photo.

The naive approach (OCR the whole page, hand the text to an LLM, ask it to split)
fails because the text arrives already scrambled: column order is lost, and the model
pairs headline A with body B. Layouts change daily, so a template can't rescue it.

This system treats it as a **layout problem first, a reading problem second**.

---

## 2. Two acquisition paths

Before any computer vision runs, the system asks: *does the publisher already give us
the text?* `epaper/probe.py` fingerprints a host against 14 vendor signatures and
returns one of four verdicts (`probe.py:88-94`):

| Verdict | Meaning | What happens next |
|---|---|---|
| `api` | A probed endpoint returned usable JSON | Take publisher text, skip CV entirely |
| `known-platform` | Vendor identified, endpoints not hit blind | A client may exist or be writable |
| `cv` | No data source found | Page images + segmentation pipeline |
| `unreachable` | Host dead / blocking / DNS failed | Nothing downstream can run |

Two platform clients are implemented (`epaper/platforms/`):

**Summit India** (`summit.py`) — the good case. The publisher's own DTP article text,
no OCR, no layout analysis. Verified on `epaper.ntnews.com` and `epaper.sakshi.com`.

- Tagging is **per-edition, not per-title**: on Namasthe Telangana the two main
  editions are fully tagged (120 articles / 12 pages) while every district edition
  returns an empty story list and falls back to CV (`summit.py:9-13`).
- Dates **must** be `DD/MM/YYYY`. `YYYY-MM-DD` silently returns an empty list.
- Telugu headline trap: headlines come back as legacy non-Unicode font bytes; the
  **body is proper Unicode** (`summit.py:103-111`).

**Readwhere** (`readwhere.py`) — Financial Express, The Statesman, The Tribune,
Jansatta, Dainik Tribune. No login needed for what's implemented.

- Readwhere gives **no** article segmentation. Its `clips` are user-made clippings and
  come back empty (`readwhere.py:12-15`). These pages must go through CV.
- **The two-layer trap** (`readwhere.py:130-156`): each tile position carries *two*
  files — an opaque `.jpg` background (photos, display ads) and a transparent `.png`
  overlay holding **all editorial text**. Composite the JPEG alone and you get a page
  with pictures and no words, which looks exactly like a paywall. `acquire.py:42-90`
  pastes the base, then alpha-composites the text layer.

**Resolution rule** (`acquire.py:1-12`): body text in an Indian broadsheet is ~7pt.
Below roughly 2000px page width, OCR quality falls off a cliff regardless of engine.

---

## 3. The segmentation pipeline

`epaper/local_backend.py` is the real path. Three stages:

```
full page ──► DocLayout-YOLO ──► numbered elements ──► title-anchored grouping
                                                              │
                                                              ▼
                                             articles ──► crop ──► VLM read
```

**Stage 1 — detect elements.** DocLayout-YOLO (DocStructBench checkpoint) at
`imgsz=1024, conf=0.20`. Returns `title`, `plain_text`, `figure`, `figure_caption`,
`table`, etc. Class `abandon` (running heads, folios) is dropped, as is any box under
8px. Reading order is banded: sort by `(y0 // 60, x0)` (`local_backend.py:111-147`).

The detector labels **parts, not stories**. It will never tell you which headline owns
which text block. That is the next stage's job.

**Stage 2 — title-anchored grouping** (`local_backend.py:214-270`). Every non-title
block is assigned to the nearest headline above it that covers it horizontally:

- horizontal overlap ≥ 0.25 of the narrower box
- the title must sit above (`dy ≥ -0.25 × block_height`, allowing a headline beside a standfirst)
- the title must be close enough (`dy ≤ 0.28 × page_height`) — a story doesn't run half a page below its own headline
- winner is `max(overlap×100 − dy/H×100)`, so the nearest covering headline wins

*Why not bands?* A horizontal strip of a broadsheet holds several stories side by side.
Asking a 7B model to "group these regions" made it lump all of them under whichever
headline it noticed first — one 66-element box covering most of a page
(`local_backend.py:219-223`). That pass is now dead code.

*Headline height floor:* YOLO labels bylines and section tags ("STATESMAN NEWS
SERVICE", "SPORTS") as titles. Candidates are filtered to
`height ≥ max(H × 0.008, 0.50 × median_candidate_height)` — one such byline had
anchored a 1279×1951 group (`local_backend.py:229-239`).

*Orphans are kept, never dropped* — "a missing region is a lost story"
(`local_backend.py:380-394`).

**Stage 3 — read.** Each crop goes to the VLM with a prompt that asks for headline,
subheadline, kicker, byline, dateline, location, body, photo captions,
`continued_on_page`, category and a self-reported confidence. It is explicitly told
not to summarise, not to translate, not to invent cut-off text
(`local_backend.py:501-518`).

**No silent fallback.** If a stage fails it raises `LocalBackendError`. A silent
downgrade to whitespace cutting is what produced the unusable draft clippings
(`local_backend.py:18-21`).

### The other two detectors

| Module | Status | Notes |
|---|---|---|
| `local_backend.py` | **Real path** | DocLayout-YOLO + local VLM, no API key, raises rather than degrading |
| `autosegment.py` | Fallback | Recursive XY-cut on an ink mask, no model. Self-labelled *draft quality*, stamped `detector:"geometric"`. Never reads text |
| `segment.py` | Legacy | Two Claude API passes. Needs `ANTHROPIC_API_KEY`. Also holds `crop_region()` — the only cropper in the package |

---

## 4. Topic search

`epaper/topics.py`. Pull only the stories about one subject out of a whole edition.
Two stages, because the two questions cost wildly different amounts:

**SCREEN** — batches of 25 story openings (340 chars each) in one call, answering only
"could this plausibly be about the topic?" Tuned to **over-include**: a story wrongly
dropped is gone for good; one wrongly kept costs a single assess call. On any
exception the whole batch is kept — a screening failure must not silently discard
stories (`topics.py:75-92`).

**ASSESS** — survivors go one at a time for a real judgement, returning
`{relevant, score, why, summary, entities}`. The prompt forbids rewarding keyword
overlap. A match needs `relevant == true` **and** `score ≥ min_score` (default 0.5)
(`topics.py:95-149`).

The economics: a front section holds ~60 stories. Assessing all costs 60 calls;
screening costs two and usually leaves under ten.

Both stages run on the **vision** model — `EPAPER_TEXT_MODEL` falls back to
`EPAPER_VLM`. Deliberate: a T4 holds one 7B model comfortably, not two. Loading a
separate text model pushed it to 15%/85% CPU/GPU and stalled screening for minutes
(`vlm.py:52-56`).

> The `topics.py:18` docstring says `qwen2.5:7b`. That is stale — do not pull it.

---

## 5. The web app

`web/app.py` — FastAPI + SQLite, single module. `web/ui.html` — one file, vanilla JS,
titled *Clipping Desk*, light/dark themed.

### Endpoints

| Route | Purpose |
|---|---|
| `GET /` | Serves `ui.html` from disk each request |
| `GET /api/health` | `{ready, gpu, layout, mode, vlm, vlm_ok, detail}` |
| `GET /api/papers` | The 7-entry catalogue |
| `GET /api/editions?host=` | Summit hierarchy or Readwhere titles. Memoised forever |
| `GET /api/dates?host=&title_id=` | Readwhere: real publish dates (60 back). Summit: 30 **synthetic** days, unverified |
| `POST /api/jobs/upload` | multipart `file` + `with_text`; returns `{job_id}` |
| `POST /api/jobs/fetch` | `{host, title_id, date, page_no, edition, with_text}` |
| `POST /api/jobs/topic` | `{host, title_id, date, topic, edition, max_pages, min_score}` |
| `GET /api/jobs?limit=60` | History rows |
| `GET /api/jobs/{jid}` | `{job, articles[]}` — the polling endpoint |
| `DELETE /api/jobs/{jid}` | Removes rows + job directory |
| `GET /api/media/{jid}/{name}` | Serves crops and overlays — **see the security note in DEPLOYMENT.md** |

FastAPI also auto-exposes `/docs`, `/redoc` and `/openapi.json`.

### Job model

12-hex-char id. States: `queued → running → done | error`. Progress is a free-text
`stage` column (`acquiring`, `detecting layout`, `cropping`, `reading text 3/11`,
`complete`), polled by the UI every 1.5s (pages) / 2s (topics). There is **no** numeric
percentage.

Jobs run on FastAPI `BackgroundTasks` — Starlette's shared threadpool. No queue, no
worker, no concurrency cap. (The `web/app.py:11-12` docstring saying "background
thread" is stale.)

### Storage

`web/data/jobs.db` — two tables: `jobs` (25 cols) and `articles` (17 cols). Schema
upgrades in place via best-effort `ALTER TABLE ADD COLUMN` for the topic-search fields
(`jobs.mode`, `jobs.beat`, `jobs.note`, `jobs.prefiltered`, `articles.keywords`).
Per-job files in `web/data/jobs/<jid>/`: `page.jpg`, `overlay.jpg`, `a01.jpg…`,
`regions.json`.

> `init_db()` is called **only** from `main()`. Serving via `uvicorn web.app:app`
> gives a database with no tables.

### UI tabs

1. **New** — cascading Newspaper → Edition → Date → Page, *or* a drag-and-drop
   dropzone (`.jpg .jpeg .png .pdf`; PDFs go through `pdftoppm`, **first page only**).
   Both halves have an independent "Also read the text (much slower)" checkbox.
2. **Topic search** — same selects plus a page count, a free-text topic, 9 preset
   chips (courts, politics, civic, health, education, sports, business, accidents,
   agriculture) and a **Beats** row of presets backed by a keyword vocabulary
   (currently Delhi Police, 277 English/Hindi terms). Typing shows what the topic
   expands to before the search runs, via `GET /api/expand`. An **Engine** toggle
   picks `hybrid` (keywords then model — precise, minutes) or `keywords` (lexical
   only, no relevance model call); the latter is offered only for a resolved beat,
   since a free-text query has no vocabulary to match on.
3. **Result** — five stat tiles, the annotated overlay with a colour legend, and a grid
   of clipping cards; click any crop for a lightbox.
4. **History** — 11-column table of past jobs with status tags and per-row delete.

### Catalogue (7 papers)

| Paper | Host | Lang | Platform |
|---|---|---|---|
| Sakshi | epaper.sakshi.com | te | summit |
| Namasthe Telangana | epaper.ntnews.com | te | summit |
| The Tribune | epaper.tribuneindia.com | en | readwhere |
| Dainik Tribune | epaper.dainiktribuneonline.com | hi | readwhere |
| Jansatta | epaper.jansatta.com | hi | readwhere |
| Financial Express | epaper.financialexpress.com | en | readwhere |
| The Statesman | epaper.thestatesman.com | en | readwhere |

`data/delhi_papers.json` holds 35 candidate papers and `data/gujarat_papers.json` holds
22, but those feed the **probe** only — neither is wired into the web catalogue.

---

## 6. CLI tools

| Script | Invocation | Produces |
|---|---|---|
| `discover.py` | `python3 scripts/discover.py [delhi\|gujarat] [name-filter…]` | `out/<region>_probe.json` + a verdict table |
| `fetch_delhi.py` | `python3 scripts/fetch_delhi.py [--date] [--paper] [--max-pages]` | `out/pages/<Paper>/<Edition>/<date>/pNN.jpg` + Mongo rows |
| `health.py` | `python3 scripts/health.py [--probe]` | Prerequisite checklist; exit 0 when ready |
| `segment_page.py` | `python3 scripts/segment_page.py --page PATH […]` | Crops + `out/articles_<Paper>_pNN.json` |
| `build_all_pdfs.py` | `python3 scripts/build_all_pdfs.py [--backend geometric\|local\|claude]` | `out/pdf/<Paper> <date>.pdf` |
| `build_report.py` → `build_page.py` | no flags | `out/report.html` |
| `build_pdfs.py` | no flags | Two hardcoded demo PDFs |

`health.py --probe` draws a synthetic 420×240 image reading "MINISTER RESIGNS", sends
it to the VLM and passes only if the returned headline contains MINISTER or RESIGN.
That is the one end-to-end smoke test in the repo.

---

## 7. Quality signals

`text_source` is the field to trust:

- `publisher` — exact text from the publisher's CMS, no OCR
- `vlm` / `vlm-local` — read off the page image by a vision model
- `layout-only` — boundaries known, text not extracted

Also emitted: `coverage` (fraction of detected elements assigned to some article),
`grouping_confidence` per article, and `segment.score()` — a deliberately conservative
0–1 gate that penalises a missing headline (−0.35), a body under 120 chars (−0.25), and
a headline appearing twice in its own body (−0.2, usually two articles merged).

---

## 8. Verified results

- **The Tribune**, Delhi Edition, 12 Aug 2026, p1: 2400×3694 page acquired, 8 articles
  + 1 ad + masthead separated, crops written, records in MongoDB.
- **The Statesman** p1: 11 regions, 8 named, 99% coverage.
- **Jansatta** (Hindi) p3 via the UI: 13 articles, 99% coverage, umbrella package split
  correctly.

## 9. Known limits

- **Telugu is unread.** `qwen2.5vl:7b` cannot read Telugu script. Real observed case:
  "మిల్లర్ల ముక్కు పిండండి" → model returned "మేరు సందోహాలు రెండు". Hindi works. Gujarati
  is **untested**. Sakshi topic search returned 0 of 36 for this reason — the screening
  logic itself is verified working.
- **Readwhere page 1 only** on paid titles. `pagemeta/get` takes `?user=&crypt=&key=`
  and no module passes them. Free full editions confirmed: FE (54 pages), Jansatta (44),
  Statesman (12).
- **Byline rejection is inert.** `rejected = set()` is hardcoded
  (`local_backend.py:344`). Enabling it lost "Tribunals Reform Bill" from Statesman p1
  and dropped coverage 99% → 95%, because removing byline-shaped titles shifts the
  median that sets the height floor. A proper fix computes the floor *before* rejection.
- **Gujarat is acquisition-only.** `discover.py gujarat` works; there is no Gujarat
  fetch script. Of the 22 titles, only Ahmedabad Mirror is Readwhere and works today.
  Gujarat Samachar, Sandesh, Akila, Phulchhab and Mumbai Samachar are bespoke platforms
  with no client.
- **No OCR engine anywhere.** `store.py` documents `text_source="ocr"` but nothing
  produces it.
- **No test suite.** 28 tracked files, zero tests.
- **Licensing is unresolved.** These are paid, copyrighted publications.
