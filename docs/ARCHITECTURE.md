# Architecture

E-Paper article extraction — layered document-object-detection pipeline.

**Governing principle: solve the geometry before reading a word.** Layout detection finds
*parts*, a deterministic rule assembles them into *stories*, and only then does a model
read one finished story at a time. The detector is never asked to understand the page,
and the language model is never asked which text belongs to which headline.

|                       |                                          |
| --------------------- | ---------------------------------------- |
| **Layout**      | DocLayout-YOLO (YOLOv10, DocStructBench) |
| **Reading**     | Qwen2.5-VL 7B via Ollama                 |
| **Association** | Deterministic geometry — no model       |
| **API**         | FastAPI (Python 3.9)                     |
| **UI**          | React 19 + TypeScript, Vite              |
| **Store**       | SQLite + filesystem                      |
| **Compute**     | Tesla T4                                 |

---

## 0. The flow at a glance

```mermaid
flowchart TD
    A["E-Paper PDF / Image"] --> B["Page Rendering<br/><b>poppler · pdftoppm</b><br/><i>PDF → image, 200 DPI</i>"]
    B --> C["Image Preprocessing<br/><b>Pillow</b><br/><i>normalise → RGB JPEG</i>"]
    C --> D["Layout Detection<br/><b>DocLayout-YOLO</b><br/><i>find every box + its type</i>"]
    D --> E["Reading Order<br/><b>band sort</b><br/><i>y-bands, then left→right</i>"]
    E --> F["Headline Reading<br/><b>Qwen2.5-VL</b><br/><i>read title crops only</i>"]
    F --> G["Region Association<br/><b>title anchoring</b><br/><i>plain geometry, no model</i>"]
    G --> H["Region Crop<br/><b>Pillow</b><br/><i>cut at full resolution</i>"]
    H --> I["Text Extraction<br/><b>Qwen2.5-VL</b><br/><i>one article at a time</i>"]
    I --> J["Structured Newspaper<br/><i>JSON + crops + bboxes</i>"]

    classDef ai fill:#ECF6F0,stroke:#1F7A4D,stroke-width:2px,color:#15181D
    classDef plain fill:#F4F4F2,stroke:#767E8E,color:#15181D
    classDef io fill:#EDF4FB,stroke:#1B7FD4,stroke-width:2px,color:#15181D
    class A,J io
    class D,F,I ai
    class B,C,E,G,H plain
```

**Green = a model runs. Grey = ordinary code.**

Two things distinguish this from the usual layout-plus-OCR design:

- **OCR does not run in parallel with layout detection.** It runs *after* association, on
  one finished article at a time. Reading the whole page up front is what produces
  interleaved columns and mismatched headlines.
- **Association is not a model.** It is geometry — which makes it deterministic and
  debuggable.

---

## 1. System architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│  CLIENT LAYER                                                        │
│    React 19 + TypeScript SPA                                         │
│    relative /api paths — no API host configured anywhere              │
└───────────────────────────────┬──────────────────────────────────────┘
                                │  HTTP, same origin
┌───────────────────────────────▼──────────────────────────────────────┐
│  APPLICATION LAYER · FastAPI                                         │
│                                                                      │
│    REST API (11 routes) ──► Job Orchestrator ──► SQLite  jobs        │
│                                    │                     articles     │
│                                    └──────────► File Store           │
│                                                  page · overlay      │
│                                                  crops · regions     │
└───────────────────────────────┬──────────────────────────────────────┘
                                │
┌───────────────────────────────▼──────────────────────────────────────┐
│  ACQUISITION LAYER                                                   │
│                                                                      │
│    Platform Probe  ──┬──► Publisher-JSON Client ─────────────┐       │
│    14 signatures     │       exact CMS text                  │       │
│                      │                                       │       │
│                      └──► Tile-Pyramid Client                │       │
│                              two-layer composite             │       │
│                                                              │       │
│                           Direct Upload (image / PDF)        │       │
└───────────────────────────────┬──────────────────────────────┼───────┘
                                │                              │
┌───────────────────────────────▼──────────────────────────────┼───────┐
│  PROCESSING LAYER                                            │       │
│                                                              │       │
│    1 · Layout Detection      DocLayout-YOLO         [GPU]    │       │
│              │                                               │       │
│    2 · Headline Read         Qwen2.5-VL             [GPU]    │       │
│              │                                               │       │
│    3 · Title Anchoring       deterministic geometry [CPU]    │       │
│              │                                               │       │
│    4 · Region Crop           full resolution        [CPU]    │       │
│              │                                               │       │
│    5 · Transcription         Qwen2.5-VL             [GPU]    │       │
└──────────────┬───────────────────────────────────────────────┼───────┘
               │                                               │
               │            ┌──────────────────────────────────┘
               │            │  bypasses the entire vision pipeline
┌──────────────┼────────────▼─────────────────────────────────────────┐
│  TOPIC LAYER │                                                      │
│              │      Screen  (batch of 25, recall-biased)            │
│              │         │                                            │
│              │      Assess  (individual, scored + reasoned)         │
└──────────────┼────────────┬─────────────────────────────────────────┘
               │            │
┌──────────────▼────────────▼─────────────────────────────────────────┐
│  STRUCTURED OUTPUT                                                  │
│    headline · body · byline · dateline · category · bbox · crop     │
└─────────────────────────────────────────────────────────────────────┘
```

Same diagram, rendered:

```mermaid
flowchart TB
  subgraph CLIENT["CLIENT LAYER"]
    UI["React 19 + TypeScript SPA<br/><i>relative /api paths</i>"]
  end

  subgraph APP["APPLICATION LAYER · FastAPI"]
    API["REST API<br/>11 routes"]
    ORCH["Job Orchestrator<br/><i>queued → running → done</i>"]
    DB[("SQLite<br/>jobs · articles")]
    FS[("File Store<br/>page · overlay · crops")]
    API --> ORCH
    ORCH --> DB
    ORCH --> FS
  end

  subgraph ACQ["ACQUISITION LAYER"]
    PROBE["Platform Probe<br/><i>14 vendor signatures</i>"]
    SUMMIT["Publisher-JSON Client"]
    RW["Tile-Pyramid Client<br/><i>2-layer composite</i>"]
    UPL["Direct Upload<br/><i>image / PDF</i>"]
    PROBE --> SUMMIT
    PROBE --> RW
  end

  subgraph PROC["PROCESSING LAYER"]
    L1["1 · Layout Detection<br/>DocLayout-YOLO"]
    L2["2 · Headline Read<br/>Qwen2.5-VL"]
    L3["3 · Title Anchoring<br/><i>deterministic geometry</i>"]
    L4["4 · Region Crop<br/>full resolution"]
    L5["5 · Transcription<br/>Qwen2.5-VL"]
    L1 --> L2 --> L3 --> L4 --> L5
  end

  subgraph TOPIC["TOPIC LAYER"]
    T1["Screen<br/><i>batch 25, inclusive</i>"]
    T2["Assess<br/><i>individual, scored</i>"]
    T1 --> T2
  end

  OUT["STRUCTURED OUTPUT<br/>headline · body · byline · bbox · crop"]

  UI --> API
  ORCH --> ACQ
  SUMMIT -.->|"publisher text —<br/>bypasses all CV"| TOPIC
  RW --> L1
  UPL --> L1
  L5 --> OUT
  L4 --> T1
  T2 --> OUT

  classDef client fill:#EDF4FB,stroke:#1B7FD4,stroke-width:2px,color:#15181D
  classDef app fill:#F2EDFB,stroke:#7C5BD4,stroke-width:2px,color:#15181D
  classDef acq fill:#FBF2E9,stroke:#C9701C,stroke-width:2px,color:#15181D
  classDef gpu fill:#ECF6F0,stroke:#1F7A4D,stroke-width:2px,color:#15181D
  classDef cpu fill:#F4F4F2,stroke:#767E8E,stroke-width:2px,color:#15181D
  classDef out fill:#FFFFFF,stroke:#15181D,stroke-width:2px,color:#15181D

  class UI client
  class API,ORCH,DB,FS app
  class PROBE,SUMMIT,RW,UPL acq
  class L1,L2,L5,T1,T2 gpu
  class L3,L4 cpu
  class OUT out
```

> **The design decision that matters.** Stage 3 is **not a model**. Article association is
> solved with geometry, not inference — deterministic, instant, and inspectable when it
> is wrong. An earlier version asked the VLM to group regions; it merged 66 elements into
> a single "article" covering most of a page.

---

## 2. Deployment topology

```
   BROWSER                APPLICATION HOST              INFERENCE HOST · Tesla T4
                                                      
  ┌──────────┐          ┌──────────────────┐          ┌─────────────────────────┐
  │  React   │  HTTP    │  FastAPI :8080   │  POST    │  layout_service.py :8001│
  │   SPA    │ ───────► │  + static build  │ ───────► │  DocLayout-YOLO         │
  │          │  /api/*  │                  │ /layout  │                         │
  └──────────┘  same    │        │         │          ├─────────────────────────┤
                origin  │        ▼         │  POST    │  Ollama :11434          │
                        │  ┌──────────┐    │ ───────► │  qwen2.5vl:7b           │
                        │  │  SQLite  │    │ /api/chat│                         │
                        │  └──────────┘    │          └─────────────────────────┘
                        └──────────────────┘
                                              SSH port-forward
                                              (never publicly exposed)
```

```mermaid
flowchart LR
  subgraph B["BROWSER"]
    BR["React SPA<br/><i>served as static build</i>"]
  end
  subgraph M["APPLICATION HOST"]
    WEB["FastAPI :8080<br/>+ static frontend"]
    SQL[("SQLite")]
    WEB --- SQL
  end
  subgraph G["INFERENCE HOST · Tesla T4"]
    LS["layout_service.py :8001<br/>DocLayout-YOLO"]
    OL["Ollama :11434<br/>qwen2.5vl:7b"]
  end
  BR -->|"HTTP · same origin"| WEB
  WEB -->|"POST /layout"| LS
  WEB -->|"POST /api/chat"| OL

  classDef b fill:#EDF4FB,stroke:#1B7FD4,stroke-width:2px,color:#15181D
  classDef m fill:#F2EDFB,stroke:#7C5BD4,stroke-width:2px,color:#15181D
  classDef g fill:#ECF6F0,stroke:#1F7A4D,stroke-width:2px,color:#15181D
  class BR b
  class WEB,SQL m
  class LS,OL g
```

**The inference host holds no application code.** Image in, bounding boxes out. It has no
concept of a newspaper, a job or an article.

| Concern          | Mechanism                                                                                                  |
| ---------------- | ---------------------------------------------------------------------------------------------------------- |
| Frontend → API  | Same-origin relative paths. The SPA build is served by FastAPI itself — no API host to configure, no CORS |
| API → inference | Plain HTTP POST over stdlib`urllib`. No SDK, no RPC framework                                            |
| Transport        | SSH port-forward; inference ports are never publicly exposed                                               |
| Mode switch      | `EPAPER_LAYOUT_URL` set = remote inference, unset = in-process. No code change                           |
| Concurrency      | Background tasks on the API process —**no queue yet**                                               |

---

## 3. Processing stages

| # | Stage            | In → Out                    | Technology                                   | Runs on       |
| - | ---------------- | ---------------------------- | -------------------------------------------- | ------------- |
| 0 | Acquisition      | source →`page.jpg`        | Tile compositing / HTTP / poppler            | CPU           |
| 1 | Layout detection | page → labelled boxes       | DocLayout-YOLO,`imgsz 1024`, `conf 0.20` | **GPU** |
| 2 | Headline read    | title crops → headline text | Qwen2.5-VL 7B                                | **GPU** |
| 3 | Title anchoring  | boxes → article groups      | **Deterministic geometry**             | CPU           |
| 4 | Region crop      | groups → article images     | Pillow, full resolution                      | CPU           |
| 5 | Transcription    | crop → structured fields    | Qwen2.5-VL 7B                                | **GPU** |
| 6 | Persistence      | fields → rows + files       | SQLite + filesystem                          | CPU           |

### Reading order

Banded: sort by `y0 // 60`, then by `x0` — top-to-bottom in horizontal bands, left-to-right
within each band. Class `abandon` (running heads, folios) and boxes under 8px are dropped.

### Article association

Every non-title region is assigned to the nearest title above it that covers it
horizontally, subject to three constraints:

- horizontal overlap ≥ **25%** of the narrower box
- the title is above — tolerating slight inversion for headline-beside-standfirst
- vertical gap ≤ **28%** of page height

Score is `overlap − normalised distance`; the nearest covering title wins. Title
candidates below `max(H × 0.008, 0.50 × median)` are demoted, because the detector labels
bylines and section tags as titles. Unassigned regions are retained as **orphans** rather
than discarded — a dropped region is a lost story.

```
        ┌──────────────────────────────────────────────────┐
        │        MASTHEAD                                  │
        ├───────────────────────┬──────────────────────────┤
        │  HEADLINE A           │  HEADLINE B              │
        │  ┌─────────────────┐  │  ┌────────────────────┐  │
        │  │ body            │  │  │ body               │  │
        │  └─────────────────┘  │  └────────────────────┘  │
        │  ┌─────────────────┐  │  ┌────────────────────┐  │
        │  │ body            │  │  │  figure            │  │
        │  └─────────────────┘  │  └────────────────────┘  │
        └───────────────────────┴──────────────────────────┘

   Left blocks   → HEADLINE A is above AND overlaps horizontally
   Right blocks  → HEADLINE B  (A is above, but does not overlap)
```

> **Failure policy.** Any stage failure raises and marks the job errored. There is **no
> silent fallback** to the cruder geometric detector — an earlier fallback produced 697
> plausible-looking but unusable clippings that went unnoticed.

---

## 4. Topic retrieval

```mermaid
flowchart LR
  A["Edition articles<br/>~60"] --> B["SCREEN<br/>batch of 25<br/>recall-biased"]
  B --> C["Shortlist<br/>~8"]
  C --> D["ASSESS<br/>individual<br/>scored + reasoned"]
  D --> E["Matches<br/>score ≥ threshold"]

  style A fill:#F4F4F2,stroke:#767E8E,color:#15181D
  style B fill:#FBF2E9,stroke:#C9701C,stroke-width:2px,color:#15181D
  style C fill:#F4F4F2,stroke:#767E8E,color:#15181D
  style D fill:#EDF4FB,stroke:#1B7FD4,stroke-width:2px,color:#15181D
  style E fill:#ECF6F0,stroke:#1F7A4D,stroke-width:2px,color:#15181D
```

~10 model calls instead of 60.

Stage one is deliberately biased toward **recall**: a story dropped here is unrecoverable,
while a false positive costs one assess call. On any error the whole batch is kept. Stage
two supplies **precision**, and is instructed that incidental mention of a topic does not
make a story about it.

> **Known architectural gap.** Stage one currently uses the LLM. It should be a **lexical +
> vector prefilter** — query expansion (`police → FIR, constable, DSP, arrest`), BM25,
> embedding similarity — with the LLM reserved for stage two. That removes the dominant
> cost and the dependency on GPU availability.

---

## 5. Design rationale

| Decision                      | Rationale                                                                                                                       |
| ----------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| Layout before OCR             | Reading a broadsheet linearly interleaves columns; downstream splitting then fails*silently*. Geometry must be resolved first |
| Geometry for association      | Deterministic and inspectable. An LLM asked to group side-by-side stories collapses them                                        |
| VLM over classical OCR        | Returns labelled fields rather than a character stream.*Trade-off: weak on Indic scripts*                                     |
| Crop at full resolution       | Indian broadsheet body text is ~7pt; sub-2000px page width destroys legibility irrecoverably                                    |
| Publisher JSON when available | Exact CMS text, zero error surface, no compute                                                                                  |
| Self-hosted models            | No per-page API cost; data does not leave controlled infrastructure                                                             |
| Same-origin SPA               | Deployment portability — no environment-specific API host, no CORS                                                             |
| Fail loudly                   | Silent degradation produced a large volume of undetected bad output                                                             |

---

## 6. Roadmap

| Priority | Change                                                            | Addresses                                                       |
| -------- | ----------------------------------------------------------------- | --------------------------------------------------------------- |
| 1        | PaddleOCR for Indic scripts, retaining layout-derived field roles | Telugu unreadable; Gujarati unverified — blocks a target state |
| 2        | Lexical + vector prefilter ahead of LLM assess                    | Topic search latency and GPU contention                         |
| 3        | Supervised process management for inference service + tunnel      | Manual startup; no restart on failure                           |
| 4        | Fine-tune detector with`advertisement` and `byline` classes   | Bylines misclassified as titles; adverts inferred heuristically |
| 5        | Platform branch in the page-fetch path                            | Publisher-JSON papers unavailable in page extraction            |

---

## Validation status

Verified on English and Hindi editions at 99% region coverage. Region coverage measures
**segmentation**, not reading accuracy — the latter has not been measured on a
statistically meaningful sample, and there is no automated test suite.

See also: [HOW-IT-WORKS.md](HOW-IT-WORKS.md) for a plain-language explanation,
[FUNCTIONALITY.md](FUNCTIONALITY.md) for the detailed reference, and
[DEPLOYMENT.md](DEPLOYMENT.md) for the runbook.
