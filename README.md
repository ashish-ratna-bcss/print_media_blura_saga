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

The app -- web UI, API and archive -- is one command. `run.ps1` on Windows,
`run.sh` on Linux or in Git Bash; the subcommands are identical.

```powershell
.
un.ps1 setup      # once: .venv, dependencies, .env
.
un.ps1            # http://127.0.0.1:8080
```

| command | what it does |
|---|---|
| *(none)* | rebuilds the frontend if any source under `web/frontend` is newer than the bundle, then serves the SPA and the API from one process on :8080 |
| `dev` | Vite on :5173 with hot reload, backend on :8080 with `--reload`. **Open :5173** -- :8080 still serves the last build |
| `build` | the frontend bundle only |
| `setup` | `.venv`, `pip install -r requirements.txt`, `npm install`, and `.env` copied from `.env.example` |
| `check` | `scripts/db_check.py` then `scripts/health.py`, and exits |

Ports move with `-Port` / `-VitePort` in PowerShell and `PORT=` / `VITE_PORT=`
in bash; `-Bind 0.0.0.0` (`BIND=`) accepts connections from other machines.
`run.cmd` is a shim for cmd.exe, and for a PowerShell whose execution policy
refuses a `.ps1` that arrived as a download.

`setup` deliberately stops short of torch, the DocLayout weights and Ollama:
several gigabytes, and only extraction needs them. The archive, the beats, the
catalogue and topic search all run without them. See **Local backend** below.

The pipeline scripts, for the work that happens outside the UI:

```bash
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

## Topic search and the Delhi Police beat

Asking for "Delhi Police" and matching the string `police` finds a fraction of
the beat: the arrest, the FIR, the chargesheet and the DCP's statement mostly do
not use the word. `epaper/keywords.py` holds hand-built **beat vocabularies** —
277 weighted terms for Delhi Police, English and Hindi — and a query is resolved
to one before anything is searched.

Terms are weighted because they are not equally informative:

| weight | | example |
|---|---|---|
| `CORE` 1.0 | the force itself | police, constable, थाना, crime branch, DCP |
| `RELATED` 0.6 | what police do, and what is done | arrested, FIR, गिरफ्तार, दुष्कर्म, raid |
| `CONTEXT` 0.3 | the surrounding furniture | court, Tihar, अदालत, Dwarka, दिल्ली |

One CORE term deliberately does **not** clear the default 0.5 threshold on its
own — "the minister said police were informed" is a passing mention, and a
vocabulary this wide should find more than one term in a story truly on the beat.

**Geography is judged separately from weight.** Vocabulary alone cannot tell
Delhi Police from any other force: a real run scored *"Khanna police turn to
facial recognition"* at 1.00 — perfect policing language, wrong state, and The
Tribune's Delhi edition is full of Punjab copy. But requiring the word "Delhi"
would be worse, because in a Delhi edition most local stories never say it. So
a beat carries two placename lists, and a story is demoted only when it names
**somewhere else and never here**. Silence is not evidence of elsewhere.

A multiplier alone proved too weak — a dense enough out-of-region story still
cleared the bar — so `OUT_OF_REGION` scales the score for ordering and
`OUT_OF_REGION_CAP` guarantees none of them passes 0.5 on lexical evidence.
Hybrid mode is untouched: the prefilter keys on whether any term matched, not
on score, so off-patch stories still reach the model, which may disagree.

Hindi inflects by suffix, so Devanagari terms prefix-match (`थाने`, `गिरफ्तारी`
and `दबोचा` all hit) while Latin terms match whole words. Acronyms are matched
case-sensitively and with optional dots, so `FIR` and `F.I.R.` hit but "fir
trees", "the firm" and "first" do not.

This buys three things:

- **A free prefilter.** A body with zero hits against 277 terms is not on the
  beat, so it never reaches the model. It only ever *drops* an article that had
  enough text to judge and matched nothing, and it fails open — if that would
  discard the whole edition, the vocabulary is likelier wrong than the paper.
- **An answer when the model is down.** `mode=keywords` ranks lexically with no
  model call at all. On Summit papers, where the publisher supplies the text,
  that is a whole-edition search in seconds. It is recall-first and cannot tell
  a police story from a story quoting a policeman — `topics.assess` is still
  what precision comes from.
- **Evidence.** The matched terms are stored per article and shown on each
  clipping, so a result is explainable without asking a model why it said what.

A `hybrid` job whose model server has gone away degrades to `keywords` and
reports it in `note`, rather than failing.

```bash
curl localhost:8080/api/beats                        # presets and their size
curl 'localhost:8080/api/expand?topic=Delhi+Police'  # exactly what will be searched
```

Adding a beat is adding one dict to `BEATS` in `epaper/keywords.py`; a query
that resolves to no beat is searched as written, with no expansion invented for
it.

### Headline OCR, and why the text is fused

Profiling a Tribune front page put most of the extraction time in one place:
`_read_headline` sent *every* title candidate to the vision model — 18 crops,
measured at 84.6 s warm and 344 s cold. `epaper/ocr.py` reads them with
Tesseract (`eng+hin`) instead, in 5.4 s.

This is not the OCR the README argues against. That argument is about reading a
whole page in raster order, which is wrong for a newspaper by construction. By
this point YOLO has isolated one headline box, so there is no reading order left
to get wrong — only a strip of large bold type.

| mode | page | note |
|---|---|---|
| `EPAPER_HEADLINE=ocr` | 5.4 s | default |
| `EPAPER_HEADLINE=auto` | 47 s | OCR, model only on suspect output |
| `EPAPER_HEADLINE=vlm` | 84.6 s | the old path; keeps the `is_ad` flag |

`auto` was measured and rejected: only 3 of 18 crops triggered a fallback, but
at 7–20 s each they ate the saving, leaving 1.8× instead of 16×.

**The fused-word problem is not an OCR defect.** Both engines produce
`Proudoftaggiven`, `PChidambaram`, `Retestof3UGC-NET` — the spaces are lost in
the source rendering. That matters because Latin terms match on whole-word
boundaries, so a headline read as `policearrested` would score zero and the
story would vanish from its beat, which looks identical to there being no
story. So `keywords.py` also matches terms as substrings, but only inside runs
of `FUSED_RUN` characters or more and only for terms of `FUSED_TERM` or more.
Both limits are load-bearing: without them `cop` matches inside `copy`.
Acronyms are excluded entirely, so `FIR` still cannot fire on `firm`.

**What OCR costs:** the VLM prompt also returned an `is_ad` flag and Tesseract
cannot. Ad detection falls back to the geometric figure-ratio test alone, which
is weaker. That is the reason `vlm` mode still exists.

Tesseract needs `hin.traineddata` in its `tessdata` directory for Devanagari;
without it the module reports the missing language and reads English only
rather than failing every call.

### Tuning the vocabulary without a GPU

`scripts/topic_check.py` scores article text against a beat and shows which
terms fired, in which weight tier. It imports nothing but `keywords.py` — no
layout model, no VLM, no network, no database — so the term list can be checked
against real Hindi and English copy on any machine.

```bash
python3 scripts/topic_check.py --demo              # built-in EN/HI examples
python3 scripts/topic_check.py --json arts.json    # [{headline, body}, ...]
python3 scripts/topic_check.py --text stories.txt  # blocks split on --- or blank line
cat story.txt | python3 scripts/topic_check.py
python3 scripts/topic_check.py --terms             # dump the vocabulary by tier
```

`DROP` rows are what the prefilter would hide from the model entirely, so a real
story appearing there is a hole in the vocabulary. This is how the Devanagari
rank spellings (`एसएचओ`, `डीसीपी`) were found missing: the Latin acronyms are
matched case-sensitively and never caught them.

## Configuration

Everything is read from the environment, and the environment can be primed
from a gitignored `.env` at the repo root:

```bash
cp .env.example .env          # then edit
python scripts/db_check.py    # confirm it resolved to what you meant
```

A real environment variable always beats `.env`, so a container or systemd
unit overrides the file without editing it. `db_check.py` reports which source
each setting came from, because the one failure this precedence causes is an
edit to `.env` that silently does nothing.

| variable | default | |
|---|---|---|
| `EPAPER_MONGO` | `mongodb://localhost:27017` | connection string |
| `EPAPER_MONGO_DB` | `epaper_corpus` | the database this project owns |
| `EPAPER_MONGO_TEST_DB` | `epaper_corpus_test` | where the tests write |
| `EPAPER_MONGO_TIMEOUT_MS` | 4000 local, 15000 remote | server selection |
| `EPAPER_MONGO_APPNAME` | `epaper-corpus` | shown in the Atlas connection list |

### MongoDB Atlas

```
EPAPER_MONGO=mongodb+srv://USER:PASSWORD@CLUSTER.xxxxxxx.mongodb.net/?retryWrites=true&w=majority
EPAPER_MONGO_DB=epaper_corpus
```

`mongodb+srv://` needs `dnspython`, which is in `requirements.txt`. The older
seed-list form (`mongodb://host-shard-00-00...,...`) works without it. A
password containing `@ : / ? # [ ] %` must be percent-encoded.

Two things about Atlas that cost an afternoon each:

**An unlisted IP times out, it does not refuse.** A connection from an address
missing from Network Access fails identically to the cluster being down, which
sends people to check the password. `db_check.py` names it.

**The database at the end of the connection string is ignored, deliberately.**
Atlas appends whichever database was selected in the UI when you clicked
Connect, and on a shared cluster that belongs to someone else. Nothing calls
`get_default_database()`; the database is `EPAPER_MONGO_DB` and nothing else,
and `epaper/config.py` refuses `admin`, `local` and `config` outright. A
pasted URI cannot steer writes into a neighbouring tenant.

Give the credential `readWrite` on that one database, not `atlasAdmin` or
`readWriteAnyDatabase`. `db_check.py` prints the roles the server thinks the
connection holds and flags any that reach further than the corpus.

```bash
python scripts/db_check.py --write     # prove writes work, then undo them
python scripts/db_rename.py --from epaper --to epaper_corpus
```

MongoDB has no `renameDatabase`, so `db_rename.py` copies collection by
collection, rebuilds indexes from the source's own definitions — the text
index carries `default_language="none"` for the bilingual corpus, and a copy
that lost it would stem Devanagari as English — verifies the counts, and drops
the source only when told to.

## The corpus: extract once, search forever

The pipeline was extract-on-query. Every topic search re-fetched the page,
re-ran layout, re-grouped, re-read and re-judged — 154 to 475 s measured — and
did it all again for the next question about the same page. Reading a page is
expensive and its answer never changes.

`epaper/corpus.py` writes what extraction produced into MongoDB and queries
that instead:

```
epapers         one issue: title, edition, date, page count
pages           one printed page: image path, dimensions
articles        one story: full_text, category, keywords[], regions[], bbox
search_history  what was asked, what came back, how long it took
```

Two retrieval paths, because the two kinds of query are not alike. A resolved
beat expands to a known vocabulary, so retrieval is an indexed
`keywords: {$in: [...]}` over terms computed at ingest. A free-text query has no
vocabulary, so it falls back to the `$text` index over `full_text`.

```bash
curl 'localhost:8080/api/search?q=Delhi+Police&date_from=2026-08-01'
curl 'localhost:8080/api/search?q=dimagi+naxal&paper=The+Tribune'
curl localhost:8080/api/corpus                      # what is indexed
curl -X POST localhost:8080/api/corpus/backfill     # index past jobs
curl -X POST localhost:8080/api/corpus/rescore      # after a vocabulary change
```

**Beat scores are precomputed at ingest**, not at query time. For a resolved
beat the score depends only on the article, so recomputing it per query was
waste: at 5,000 articles, 350 ms of a 367 ms query was 292 regexes re-run over
200 candidates while Mongo itself took 3.6 ms. Storing them and sorting on an
index took that to **3.6 ms**. The trade is staleness — edit `keywords.py` and
the stored numbers are wrong until `/api/corpus/rescore` runs.

Two collisions to know about, because `store.py` and `corpus.py` write the same
collections with different field names. `store.py` holds a **unique** index on
`articles.article_id` and another on `pages.(paper, edition, date, page_no)`.
Corpus documents populate both naming schemes rather than dropping indexes a
sibling module relies on; leaving them unset makes every document after the
first collide on nulls. Ingest is also per-document, so one rejected write
costs one article rather than the whole page.

## Beats: one per force, editable at runtime

A beat is a weighted bilingual vocabulary that a query resolves to, so asking
for "Punjab Police" searches the words the beat is actually written in rather
than the string you typed. There are **36 of them — every state and union
territory** — and they live in MongoDB, so terms can be added without a deploy.

```bash
python scripts/seed_beats.py            # import all 36 (the app also does this)
python scripts/seed_beats.py --list     # what is stored
python scripts/seed_beats.py --check "Punjab Police"
python scripts/test_beats.py --db       # regression suite, no model or GPU
```

The data is factored the way the domain is, and that shape drives everything
else:

```
beat_packs.policing    ~290 policing terms. Identical in every state —
                       a constable is a constable in Kohima and in Kochi.
beats.<state>-police   what varies: the force's name, aliases, and geography.
```

So a beat is the pack plus one state's places, and **`region_out` is derived,
never stored**: it is the union of every other beat's places, computed at load.
Adding a district to Punjab makes it count as elsewhere for the other 35 on the
next reload, with no fan-out write and nothing to keep in sync.

### Adding what was missed

```bash
# a policing word — every force gets it
curl -X POST localhost:8080/api/beats/delhi-police/terms \
  -H 'Content-Type: application/json' \
  -d '{"scope":"pack","tier":"related","terms":["e-challan"]}'

# something specific to one force
curl -X POST localhost:8080/api/beats/maharashtra-police/terms \
  -H 'Content-Type: application/json' \
  -d '{"scope":"beat","tier":"related","terms":["tapori"]}'

# geography
curl -X POST localhost:8080/api/beats/punjab-police/places \
  -H 'Content-Type: application/json' -d '{"places":["Mullanpur"]}'

curl -X POST localhost:8080/api/corpus/rescore    # apply to stored articles
```

Edits take effect on the next request. Stored articles keep the scores they
were written with until `rescore` runs — that is the standing trade for
precomputing them.

**A place name that is also an ordinary word produces false geography**, and it
is expensive: it marks an on-patch story as somebody else's and drops it below
threshold. `beatdata.py` documents every name excluded for this reason. Three
are Devanagari-only exclusions, because Devanagari terms prefix-match:
`गया` (Gaya) is also the commonest Hindi past tense, so `किया गया` — "was done",
in virtually every Hindi crime report — had Bihar scoring 1.00 on a Mumbai
story. `दमन` is also "repression"; `सूरत` is also "face". Their English
spellings are unambiguous and stay. `scripts/test_beats.py` guards this.

### Two things that had to change to reach 36

**Resolution takes the longest matching alias.** Every state beat answers to
the bare word "police", so first-match sent "Punjab Police" to Delhi — "police"
matched inside it, and Delhi led the table. Longest-alias-wins makes
`punjab police` beat `police`, while a bare `police` still falls through to
whichever beat leads.

**Scoring shares the pack.** Scoring 36 beats independently re-ran the same
~290 patterns 36 times per article. `keywords.score_all()` runs the pack once
and adds only each beat's own places and its region gate: **3.9 ms instead of
30.5 ms per article**, so 36 beats now cost 4.9× a single beat rather than 36×.
The optimisation must not change results, so `test_beats.py` scores every beat
both ways and requires them to match exactly.

Retrieval changed with it. The corpus filters on `beats` — the forces whose
patch an article is actually on — rather than on the term list, which with one
shared vocabulary would match every police story in the country and then sort
90% of them to the bottom. The per-beat sort indexes are gone: 36 of them would
sit against MongoDB's limit of 64 per collection and break outright the first
time someone added a beat from the backend. Set `EPAPER_BEAT_INDEX=<id>,<id>`
to give a high-volume beat its own index back.

If MongoDB is unreachable the 36 built-in beats compiled into the code still
serve every search; only editing is unavailable. `/api/beats/<id>` reports
which of the two is in play.

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

## Browsing by day and topic

Search answers "find me stories about X". Browse answers the other question --
"what was in the paper yesterday" -- and it is the one most readers actually
have. Every article is labelled at ingest, so the whole journey is indexed
reads and returns in milliseconds:

```
GET /api/corpus/dates                       # days held, with counts
GET /api/facets?date=2026-08-17             # topics that day, with counts
GET /api/browse?date=2026-08-17&category=crime   # the clippings, printed order
GET /api/browse?date=2026-08-17&tag=cybercrime   # narrowed by keyword
```

Topics are **not a fixed list.** A model reads each article and names the
subject in its own words, so the list describes what the newspapers published
rather than what somebody predicted. Two mechanisms keep that usable:

- `epaper/catdata.py` folds away variants that carry no meaning -- case,
  spacing, punctuation -- so "Cyber Crime", "cyber-crime" and "CYBERCRIME" are
  one entry before anybody sees them. Hindi labels fold to their English
  equivalents, or a bilingual corpus splits every subject in two.
- `epaper/categories.py` is the review queue for what normalisation cannot
  know. Where two entries are genuinely one subject, merging moves the articles
  already filed *and* redirects future ones. Nothing is deleted, so the
  decision stays visible and reversible.

A person can also set an article's topic directly, from the Browse tab or
`PATCH /api/article/{uid}`. That is marked as human and **survives every later
bulk relabel** -- otherwise the next overnight run would quietly undo it.

### Collecting every night

```
python scripts/daily.py --list          # registered titles, last collection
python scripts/daily.py --dry-run       # what tonight would fetch
python scripts/daily.py                 # collect today
python scripts/daily.py --backfill 7    # the last week, oldest first
```

Titles come from the `sources` collection (`POST /api/sources`), not from
arguments. The run is idempotent -- it asks "what have I not collected for this
date", so firing twice costs two database reads and a missed night is still
visible the next evening. One paper failing does not stop the others; the
reason lands in `last_error` and `fail_count`.

### Is the labelling any good?

```
python scripts/classify_check.py --stability 20   # consistency, no human input
python scripts/classify_check.py --sample 60      # draw a set to grade by hand
python scripts/classify_check.py --score sample.json
```

Run `--stability` first. It needs no human input and puts a ceiling on
accuracy: a model that contradicts itself cannot be right. Measured at **95%**
on `qwen2.5vl:3b`. Accuracy against human judgement is not yet measured.

## Tests

```
pip install pytest && pytest
```

108 tests over label normalisation, classifier batching and alignment, the
topic registry, the browse path, the source scheduler and the HTTP contract.
They call no model and never touch the live corpus -- anything needing MongoDB
gets a scratch database, and they skip cleanly when none is reachable. See
`tests/README.md`, which also records two things the suite found out about the
code that are worth knowing before writing more.

The vision pipeline has no automated coverage; it needs page fixtures and a GPU.

## Data model

MongoDB `epaper_corpus` database (`EPAPER_MONGO_DB`), collections
`papers` / `pages` / `articles` / `categories` / `sources`.

Two fields answer two different questions about an article and are **not**
interchangeable:

| field | question | set by |
|---|---|---|
| `category` | what it is ABOUT -- the topic | `epaper/classify.py`, or a person |
| `primary_beat` | where it BELONGS -- geography and desk | `epaper/keywords.py`, free |

Both were once written under the name `category`, so a corpus of state-police
beats answered every question about subject matter with "delhi-police". An
empty `category` means nobody has classified the article yet; `unclassified`
means the classifier ran and could not label it -- a backlog and a quality
problem are not the same thing, and only one is fixed by waiting.

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
