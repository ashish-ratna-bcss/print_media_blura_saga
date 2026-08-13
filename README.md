# E-Paper Collection

Extract individual news articles from daily e-papers, whatever the layout.

The failure this project exists to fix is mismatched content — headline of
article A stitched onto the body of article B. That is not an OCR problem. OCR
reads a page in raster order; a newspaper is not one column of text, so raster
order is wrong by construction. The fix is to establish article boundaries
*before* any text is read.

## What was actually found

Two very different situations, and which one you are in decides everything.

**Some publishers hand you the articles.** Namasthe Telangana
(`epaper.ntnews.com`) runs the Summit India platform and serves an
unauthenticated JSON API containing the article text its own DTP system
produced — exact Unicode, plus byline, dateline, photo captions, article crop
images, and continued-on-page links already resolved. Zero OCR needed.
`epaper.sakshi.com` exposes the identical routes. Coverage is uneven and must
be checked per edition: NT's two main editions are fully tagged (120 articles
across 12 pages) while every district edition returns nothing.

**Delhi publishers do not.** Probing 35 Delhi-NCR titles found *no* publisher
article segmentation anywhere. Readwhere (Financial Express, The Statesman,
The Tribune, Jansatta, Dainik Tribune) serves page images and nothing else —
its "clips" are user-created bookmarks and come back empty, and there is no
text layer. For Delhi the CV/VLM pipeline is mandatory, not optional.

Full probe results: `out/delhi_probe.json`.

## Two gotchas that cost real time

**Readwhere pages are two layers.** Each tile position has a `.jpg` and a
`.png` at the same coordinates. The JPEG holds photographs and display ads;
the **PNG is a transparent overlay holding every word of editorial text**.
Composite both. Taking the JPEG alone yields a page with pictures and adverts
but no words, which looks exactly like a paywall and is not one.

**Summit headlines are legacy font bytes.** Bodies are clean Unicode but
`StoryContent.Headlines` is ASCII meant to be rendered through a proprietary
Telugu font (`b˜Í«$T©!` renders as `ఫ్యామిలీ!`). Either build the glyph map
once, or OCR the headline strip off the article crop — headlines are large and
bold, so recognition there is near perfect.

## Pipeline

```
discover  ->  acquire            ->  segment                    ->  store
probe.py      readwhere.py           segment.py                     store.py
              summit.py              pass 1 layout (whole page)
              acquire.py             pass 2 text (per-article crop)
```

`segment.py` splits layout from transcription on purpose. Pass 1 sends the
whole page downscaled and asks only *where* each article sits — boundaries are
visible from gutters and rules, not from reading words. Pass 2 crops each
article out of the full-resolution page and reads it alone, so 7pt body text
is legible and no neighbouring column can bleed in. Asking one call to do both
degrades both.

## Usage

```bash
pip3 install -r requirements.txt          # Pillow, pymongo

python3 scripts/discover.py               # probe every Delhi paper
python3 scripts/discover.py tribune       # or filter by name

python3 scripts/fetch_delhi.py                    # today's pages -> disk + mongo
python3 scripts/fetch_delhi.py --date 2026-08-11 --paper tribune

export ANTHROPIC_API_KEY=sk-ant-...
python3 scripts/segment_page.py --page out/pages/.../p01.jpg \
    --paper "The Tribune" --edition "Delhi Edition" --date 2026-08-12

# crop-only from a known layout; no API key, useful for building ground truth
python3 scripts/segment_page.py --page out/pages/.../p01.jpg \
    --regions out/regions_tribune_p01.json

python3 scripts/build_report.py && python3 scripts/build_page.py   # visual HTML report
python3 scripts/build_pdfs.py                                      # one PDF per paper
```

## PDF output

`scripts/build_pdfs.py` writes one A4 PDF per newspaper into `out/pdf/`, named
`<Paper> <YYYY-MM-DD>.pdf`. Page 1 is the full page as printed; every clipping
cut from it follows, each captioned with its column count, photo flag and
continued-on-page reference.

All *drawn* label text is Latin only, on purpose. This PIL build has no complex
-script shaping (`features.check("raqm")` is false), so Devanagari drawn as text
would come out as broken, unjoined glyphs. Headlines are never redrawn — they
stay as printed pixels inside the clipping images, which is correct for every
script and closer to the source anyway. Latin headlines are additionally shown
as a caption because they can be rendered safely.

Verified end to end on The Tribune Delhi Edition, 12 Aug 2026, page 1:
2400×3694 page acquired, 8 articles + 1 ad + masthead separated, crops written,
records in MongoDB.

## Local backend (no API key)

`epaper/local_backend.py` replaces both the Anthropic calls and the discredited
whitespace geometry:

```
full page -> DocLayout-YOLO -> numbered elements
          -> VLM grouping   -> articles
          -> crop + VLM read -> structured JSON
```

DocLayout-YOLO is trained on real document structure, so it finds titles, text
blocks, figures and captions where projection profiles cannot. It labels PARTS,
not STORIES though -- it will not say which headline owns which text block. That
is a reading problem, so the VLM gets the page plus the numbered boxes and is
asked only which numbers belong together. Reasoning over a dozen numbered
regions beats asking a 7B model to find articles in raw pixels.

The VLM provider is swappable (`epaper/vlm.py`): Ollama today, vLLM once the
NVML driver mismatch is resolved. Nothing else changes when that flips.

```bash
source ~/epaper-venv/bin/activate
export EPAPER_YOLO=~/models/doclayout_yolo_docstructbench_imgsz1024.pt
export EPAPER_VLM_BACKEND=ollama
export EPAPER_VLM=qwen2.5vl:7b

python3 scripts/health.py                        # check every prerequisite first
python3 scripts/build_all_pdfs.py --paper statesman --max-pages 2 --backend local
```

Per page it writes inspection artefacts to `out/debug/<Paper>/<date>/pNN/`:
`elements.json`, `elements.jpg` (YOLO boxes drawn), `grouping.json`,
`articles.jpg` (article boxes drawn), `articles.json`, and crops under
`articles_local/`. Check `articles.jpg` before running all 113 pages.

**This backend never falls back to the geometric detector.** If any stage fails
it raises, because a silent downgrade to whitespace cutting is exactly what
produced the unusable draft clippings.

## Data model

MongoDB `epaper` database, collections `papers` / `pages` / `articles`.
The field to trust when judging quality is `text_source`:

| value | meaning |
|---|---|
| `publisher` | exact text from the publisher's CMS, no OCR |
| `vlm` | read off the page image by a vision model |
| `layout-only` | boundaries known, text not yet extracted |

`confidence` (see `segment.score`) is deliberately conservative so weak
extractions can be quarantined rather than silently published.

## Known limits

- Readwhere serves **page 1 free; pages 2+ need a subscription**
  (`pagemeta/get` takes `?user=&crypt=&key=`). Paid accounts are the unblock.
- 11 of 35 Delhi hosts did not resolve — some have moved, some are SPAs whose
  edition lists need per-site handling. See `unreachable` in the probe output.
- `Readwhere.titles()` reads the Tribune theme's markup; Jansatta, FE and
  Statesman use different themes and return 0 titles. Needs a per-theme parser.
- Article bounding boxes need a few percent of padding. A box 0.3% too narrow
  clips the last column mid-word — visible in the first `a06` crop before the
  bbox was widened.
- These are paid, copyrighted publications. Scraping at scale for a commercial
  product is a licensing question regardless of the API being open; licensing a
  feed direct from the publisher also gets you the pre-layout digital text.
