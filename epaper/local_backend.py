"""
Local article extraction: DocLayout-YOLO for elements, a vision model for
grouping and reading. No API key, nothing leaves the machine.

    full page -> DocLayout-YOLO -> numbered elements
              -> VLM grouping   -> articles
              -> crop + VLM read -> structured JSON

Why three stages rather than one. DocLayout-YOLO is trained on real document
structure, so it finds titles, text blocks, figures and captions reliably where
whitespace geometry cannot -- but it labels PARTS, not STORIES. It will not tell
you which headline owns which text block. That grouping is a reading problem,
so it goes to the VLM, which is handed the page plus the numbered boxes and
asked only which numbers belong together. Reasoning over a dozen numbered
regions is far more reliable than asking a 7B model to find articles in raw
pixels.

This backend never falls back to the geometric detector. If a stage fails it
raises, because a silent downgrade to whitespace cutting is what produced the
unusable draft clippings.

Layout runs either in-process or over HTTP. Set EPAPER_LAYOUT_URL and the GPU
box becomes a pure inference server (see server/layout_service.py) while this
code stays on the laptop -- no need to ship the project or its page images
anywhere. Unset, it imports doclayout_yolo locally as before.

Environment:
    EPAPER_LAYOUT_URL    http://localhost:8001  -> use the remote layout service
    EPAPER_YOLO          weights path, when running layout in-process
    EPAPER_VLM_BACKEND   ollama | vllm      (see epaper/vlm.py)
    EPAPER_DEBUG_DIR     where per-page inspection artefacts are written
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request

from PIL import Image, ImageDraw

from . import ocr, vlm

Image.MAX_IMAGE_PIXELS = None

YOLO_W = os.path.expanduser(os.environ.get(
    "EPAPER_YOLO", "~/models/doclayout_yolo_docstructbench_imgsz1024.pt"))
DEBUG_DIR = os.path.expanduser(os.environ.get("EPAPER_DEBUG_DIR", "out/debug"))
LAYOUT_URL = (os.environ.get("EPAPER_LAYOUT_URL") or "").rstrip("/")

# DocStructBench classes, in training order.
YOLO_CLASSES = ["title", "plain_text", "abandon", "figure", "figure_caption",
                "table", "table_caption", "table_footnote", "isolate_formula",
                "formula_caption"]

ELEMENT_COLORS = {"title": (214, 51, 108),
                  "plain text": (34, 139, 230), "plain_text": (34, 139, 230),
                  "figure": (240, 140, 40), "figure_caption": (130, 90, 200),
                  "table": (32, 160, 130), "table_caption": (130, 90, 200)}

_model = None


class LocalBackendError(RuntimeError):
    pass


def _yolo():
    global _model
    if _model is None:
        if not os.path.exists(YOLO_W):
            raise LocalBackendError(
                "DocLayout-YOLO weights not found at %s. Set EPAPER_YOLO." % YOLO_W)
        try:
            from doclayout_yolo import YOLOv10
        except ImportError as ex:
            raise LocalBackendError(
                "doclayout-yolo is not installed in this interpreter (%s). "
                "Activate the venv: source ~/epaper-venv/bin/activate" % ex)
        _model = YOLOv10(YOLO_W)
    return _model


# -- stage 1: elements ----------------------------------------------------

def _remote_elements(page_path: str, conf: float) -> list:
    """Ask the GPU box's layout service. Page never leaves as a file, only bytes."""
    with open(page_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    req = urllib.request.Request(
        LAYOUT_URL + "/layout",
        data=json.dumps({"image_b64": b64, "conf": conf}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            out = json.loads(r.read().decode())
    except urllib.error.HTTPError as ex:
        raise LocalBackendError("layout service HTTP %s: %s"
                                % (ex.code, ex.read()[:300].decode("utf-8", "ignore")))
    except Exception as ex:
        raise LocalBackendError("layout service unreachable at %s: %s"
                                % (LAYOUT_URL, ex))
    if "elements" not in out:
        raise LocalBackendError("layout service returned %r" % str(out)[:200])
    return out["elements"]


def _default_device() -> str:
    """
    Prefer the GPU, but fall back to CPU instead of raising.

    This is not the kind of fallback the module refuses elsewhere. Downgrading
    the *detector* changes the answer and is banned; downgrading the *device*
    only changes how long the same answer takes. A CPU-only torch build is a
    legitimate install -- DocLayout-YOLO is small enough to run on one, and on
    a 4GB card it is arguably the better split, since it leaves the whole GPU
    to the VLM. Force either way with EPAPER_DEVICE=cpu|cuda:0.
    """
    try:
        import torch
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def detect_elements(page_path: str, conf: float = 0.20, device: str = None) -> list:
    """Document elements with pixel boxes, numbered in reading order."""
    if LAYOUT_URL:
        return _remote_elements(page_path, conf)
    device = device or os.environ.get("EPAPER_DEVICE") or _default_device()
    try:
        res = _yolo().predict(page_path, imgsz=1024, conf=conf,
                              device=device, verbose=False)[0]
    except Exception as ex:
        raise LocalBackendError("DocLayout-YOLO inference failed on %s: %s"
                                % (os.path.basename(page_path), ex))

    # Trust the checkpoint's own id->name mapping; the hardcoded list is only a
    # fallback. Getting this backwards silently mislabels every element.
    names = getattr(res, "names", None) or getattr(_yolo(), "names", None) or {}
    out = []
    for box, cls, cf in zip(res.boxes.xyxy.tolist(),
                            res.boxes.cls.tolist(),
                            res.boxes.conf.tolist()):
        i = int(cls)
        name = names.get(i) if isinstance(names, dict) else (
            names[i] if isinstance(names, (list, tuple)) and i < len(names) else None)
        if not name:
            name = YOLO_CLASSES[i] if i < len(YOLO_CLASSES) else "other"
        if name == "abandon":            # running heads, folios, page furniture
            continue
        x0, y0, x1, y1 = [int(v) for v in box]
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        out.append({"kind": name, "bbox_px": [x0, y0, x1, y1],
                    "conf": round(float(cf), 3)})

    # Reading order: banded by vertical position, then left to right.
    out.sort(key=lambda e: (e["bbox_px"][1] // 60, e["bbox_px"][0]))
    for i, e in enumerate(out, 1):
        e["n"] = i
    return out


# -- stage 2: grouping ----------------------------------------------------

GROUP_PROMPT = """This image is one full page of a printed newspaper.

A layout detector found these regions. Each has an ID like R1, and a position
given as column span and vertical span in PERCENT of the page:

%s

Decide which regions belong to the SAME news article. Do not transcribe body text.

An article = one headline plus the body text, photo and caption belonging to it.
- A story running across several columns is ONE article: list all its R-IDs.
- Body text directly below or beside a title usually belongs to that title.
- A figure and its caption belong to the article whose text sits beside them.
- If one big headline sits above several separate stories, emit ONE article per
  story and set "umbrella": true on each.
- Advertisements get "type":"ad". The paper's nameplate gets "masthead".
- Everything else is "article". Never use a region's layout label (title, plain
  text, figure) as the "type".
- Put ALL regions of one story in ONE entry. Do not emit the same headline twice.

CRITICAL: "regions" must contain ONLY the R-ID numbers listed above (the number
after the R). Never output coordinates or percentages there. Every region listed
above must appear in exactly one entry.

Return ONLY JSON with this schema. The field values below are placeholders
describing the TYPE expected -- do not copy them:
{"articles":[{"regions":[<one or more R-ID numbers>],
"type":"<article|ad|masthead>","headline":"<headline text as printed>",
"umbrella":<true|false>,"continued_on_page":<page number or null>,
"confidence":<0.0-1.0>}]}"""


def _listing(elements, W, H):
    """
    Describe regions positionally rather than in raw pixels.

    Feeding a 7B model raw [x0,y0,x1,y1] invites it to echo those numbers back
    as region ids -- observed exactly that, returning "regions":[163,248,1317].
    Percentages of the page read as geometry, not as identifiers.
    """
    rows = []
    for e in elements:
        x0, y0, x1, y1 = e["bbox_px"]
        rows.append("R%d %s | x %d-%d%% | y %d-%d%%"
                    % (e["n"], e["kind"],
                       round(x0 / W * 100), round(x1 / W * 100),
                       round(y0 / H * 100), round(y1 / H * 100)))
    return "\n".join(rows)


TITLE_KINDS = ("title",)
TEXTY = ("plain text", "plain_text", "title")


def _xov(a, b):
    """Horizontal overlap of two boxes as a fraction of the narrower one."""
    lo, hi = max(a[0], b[0]), min(a[2], b[2])
    if hi <= lo:
        return 0.0
    return (hi - lo) / float(max(1, min(a[2] - a[0], b[2] - b[0])))


def _assign_to_titles(elements, W, H):
    """
    Assign every non-title region to the title that owns it.

    Bands were the wrong unit: a horizontal strip of a broadsheet holds several
    stories side by side, so asking a 7B model to "group these regions" made it
    lump all of them under whichever headline it noticed first -- one 66-element
    box covering most of a page.

    Titles are the right unit. Each headline owns exactly one story, and the
    ownership rule a typesetter uses is simple: a block belongs to the nearest
    headline ABOVE it whose horizontal span covers it. That is geometry over
    semantic elements YOLO already identified, which is a very different and far
    easier problem than cutting whitespace on raw pixels.
    """
    cand = [e for e in elements if e["kind"] in TITLE_KINDS]
    # YOLO labels bylines and section tags ("STATESMAN NEWS SERVICE", "SPORTS")
    # as titles too. Left as anchors they swallow whole page halves -- one such
    # byline anchored a 1279x1951 group. A real headline is set in much larger
    # type, so filter on box height before anchoring.
    heights = sorted((e["bbox_px"][3] - e["bbox_px"][1]) for e in cand)
    med = heights[len(heights) // 2] if heights else 0
    floor = max(H * 0.008, 0.50 * med)
    titles = [e for e in cand if (e["bbox_px"][3] - e["bbox_px"][1]) >= floor]
    demoted = [e for e in cand if e not in titles]
    others = [e for e in elements if e["kind"] not in TITLE_KINDS] + demoted
    if not titles:
        return [], others

    groups = {t["n"]: {"title": t, "members": []} for t in titles}
    orphans = []
    for o in others:
        ob = o["bbox_px"]
        best, best_score = None, 0.0
        for t in titles:
            tb = t["bbox_px"]
            ov = _xov(tb, ob)
            if ov < 0.25:
                continue
            # Title must sit above the block, or overlap it slightly (a headline
            # beside a standfirst). Penalise distance so the NEAREST wins.
            dy = ob[1] - tb[3]
            if dy < -0.25 * (ob[3] - ob[1]):
                continue
            # A story does not run half a page below its own headline. Without
            # this cap a block whose real headline was filtered out attaches to
            # whatever headline sits above it, stretching one box down the page.
            if dy > 0.28 * H:
                continue
            score = ov * 100.0 - max(0.0, dy) / float(H) * 100.0
            if score > best_score:
                best, best_score = t, score
        if best is not None:
            groups[best["n"]]["members"].append(o)
        else:
            orphans.append(o)
    return list(groups.values()), orphans


HEADLINE_PROMPT = """This image is a headline cut from a newspaper page.

Transcribe the headline text exactly as printed, keeping the original script.
If the image is an advertisement rather than a news headline, set is_ad true.

Return ONLY JSON:
{"headline":"<text as printed>","is_ad":<true|false>}"""


def _read_headline(page_path, box, pad=8, lang=None):
    """
    Read one headline.

    This runs once per title candidate, and there are more of those than the
    "a small crop, so this is fast" comment suggested: 18 on a Tribune front
    page, ~19 s each through the vision model, which profiling put at 344 s of
    a 475 s extraction -- 72% of the whole job.

    So OCR reads it by default. The README's case against OCR is about raster
    order over a whole page, which does not apply once YOLO has isolated a
    single headline box; what is left is a strip of large bold type. Tesseract
    does that in milliseconds on CPU.

    The cost is the `is_ad` flag, which only the VLM prompt produced. Ad
    detection falls back to the geometric figure-ratio test alone, which is
    weaker. EPAPER_HEADLINE=vlm restores the old behaviour; =auto uses OCR and
    retries with the VLM only when OCR returns nothing legible.
    """
    from PIL import Image as _I
    with _I.open(page_path) as im:
        x0, y0, x1, y1 = box
        crop = im.convert("RGB").crop((max(0, x0 - pad), max(0, y0 - pad),
                                       min(im.width, x1 + pad),
                                       min(im.height, y1 + pad)))

    # "ocr" by default. "auto" was tried and abandoned: only 3 of 18 crops
    # triggered a fallback, but at 7-20 s each they ate the saving, leaving
    # 1.8x instead of 16x. The deciding evidence was that the model garbles the
    # same words -- it returned "Proud oftaggiven" and "PChidambaram" too --
    # so paying for it buys differently-broken text, not correct text. The
    # fused-run matching in keywords.py handles the damage instead, on both
    # engines' output. "auto" and "vlm" remain available.
    mode = os.environ.get("EPAPER_HEADLINE", "ocr").lower()
    if mode in ("ocr", "auto"):
        text = ocr.read_headline(crop, lang=lang)
        if mode == "ocr":
            return text, False
        if text and not ocr.looks_unreliable(text):
            return text, False

    try:
        d = vlm.chat_json(HEADLINE_PROMPT, crop, max_edge=900, max_tokens=300)
    except Exception:
        return "", False
    return (d.get("headline") or "").strip(), bool(d.get("is_ad"))


# Only explicit agency markers. An earlier version also matched "City, 11 Month"
# datelines, which swallowed real headlines -- p1 lost "Tribunals Reform Bill"
# and coverage fell from 99% to 95%. Narrow beats clever here.
BYLINE_RE = re.compile(
    r"(news service|staff reporter|our correspondent|\bbureau\b"
    r"|\bsns\b|\bpti\b|\bians\b|\bani\b)", re.I)


def _is_byline(text: str) -> bool:
    """
    Bylines and datelines are set in title-ish type, so YOLO labels them
    "title". Anchoring on one makes it swallow everything beneath -- observed a
    'STATESMAN NEWS SERVICE / New Delhi, 11 August' box 1279x1391. Height alone
    does not separate them, but their wording does.
    """
    t = " ".join((text or "").split())
    if not t:
        return False
    return bool(BYLINE_RE.search(t)) and len(t) < 60


def _figure_ratio(members, box):
    area = max(1, (box[2] - box[0]) * (box[3] - box[1]))
    fig = sum((m["bbox_px"][2] - m["bbox_px"][0]) * (m["bbox_px"][3] - m["bbox_px"][1])
              for m in members if m["kind"] == "figure")
    return fig / float(area)


def group_articles(page_path: str, elements: list, lang=None) -> list:
    """Title-anchored grouping: geometry proposes, the VLM reads each headline."""
    from PIL import Image as _I
    with _I.open(page_path) as im:
        W, H = im.size

    # Read every title candidate BEFORE anchoring, so bylines can be rejected
    # as anchors rather than demoted afterwards when the damage is done.
    texts = {}
    for e in elements:
        if e["kind"] in TITLE_KINDS:
            texts[e["n"]] = _read_headline(page_path, e["bbox_px"], lang=lang)
    # Byline rejection is DISABLED. Removing byline-shaped titles from the
    # candidate pool shifts the median that sets the height floor, which
    # changes which real headlines survive as anchors. Measured on Statesman
    # p1: enabling it lost "Tribunals Reform Bill" entirely, turned the
    # JPSC-JSSC story into a 728x1745 "ad", and dropped coverage 99% -> 95%.
    # _is_byline stays for reference; a stable fix needs the floor computed
    # before rejection, not after.
    rejected = set()
    usable = [e for e in elements
              if e["kind"] not in TITLE_KINDS or e["n"] not in rejected]

    groups, orphans = _assign_to_titles(usable, W, H)
    if not groups:
        raise LocalBackendError("no titles detected on %s; cannot anchor articles"
                                % os.path.basename(page_path))

    arts = []
    for g in groups:
        t = g["title"]
        members = g["members"]
        boxes = [t["bbox_px"]] + [m["bbox_px"] for m in members]
        box = [min(b[0] for b in boxes), min(b[1] for b in boxes),
               max(b[2] for b in boxes), max(b[3] for b in boxes)]
        headline, is_ad = texts.get(t["n"], ("", False))
        kinds = sorted({m["kind"] for m in members} | {t["kind"]})
        figr = _figure_ratio(members, box)
        kind = "ad" if (is_ad or figr > 0.65) else "article"
        if box[1] < H * 0.06 and (box[2] - box[0]) > W * 0.55:
            kind = "masthead"
        arts.append({
            "type": kind,
            "headline_visible": headline,
            "continued_on_page": None,
            "package": False,
            # Per-element boxes, kept rather than collapsed into the article
            # bbox. The union box is enough to crop, but not enough to point a
            # reader at the headline inside it, and this is the only place the
            # member geometry still exists. `text` is filled for the headline
            # only -- body text per region needs OCR, which does not run here.
            "region_boxes": (
                [{"type": "headline", "bbox": list(t["bbox_px"]), "text": headline}]
                + [{"type": m["kind"], "bbox": list(m["bbox_px"]), "text": ""}
                   for m in members]),
            "elements": sorted([t["n"]] + [m["n"] for m in members]),
            "element_kinds": kinds,
            "has_photo": any(m["kind"] == "figure" for m in members),
            "grouping_confidence": round(min(1.0, 0.4 + 0.1 * len(members)), 2),
            "figure_ratio": round(figr, 2),
            "bbox_px": box,
            "detector": "doclayout-yolo(titles)+%s" % vlm.MODEL,
        })

    # Orphaned blocks -- no headline above them -- are usually standalone photos
    # or ad copy. Emitted rather than dropped: a missing region is a lost story.
    for o in orphans:
        b = o["bbox_px"]
        if (b[2] - b[0]) < W * 0.05 or (b[3] - b[1]) < H * 0.015:
            continue
        arts.append({
            "type": "ad" if o["kind"] == "figure" else "article",
            "headline_visible": "", "continued_on_page": None, "package": False,
            "elements": [o["n"]], "element_kinds": [o["kind"]],
            "has_photo": o["kind"] == "figure", "grouping_confidence": 0.2,
            "figure_ratio": 1.0 if o["kind"] == "figure" else 0.0,
            "bbox_px": list(b), "unassigned": True,
            "detector": "doclayout-yolo(orphan)",
        })

    # A rejected byline is not lost: it becomes a body block of whatever
    # article's box already contains it.
    by_n = {e["n"]: e for e in elements}
    for n in rejected:
        b = by_n[n]["bbox_px"]
        cx, cy = (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0
        for a in arts:
            ab = a["bbox_px"]
            if ab[0] <= cx <= ab[2] and ab[1] <= cy <= ab[3]:
                a["elements"] = sorted(set(a["elements"]) | {n})
                break

    arts = _merge_by_headline(arts, by_n)
    seen = {n for a in arts for n in a["elements"]}
    cov = round(len(seen) / float(max(1, len(elements))), 3)
    for a in arts:
        a["coverage"] = cov
    arts.sort(key=lambda a: (a["bbox_px"][1], a["bbox_px"][0]))
    return arts


VALID_TYPES = ("article", "ad", "masthead")


def _norm_headline(h):
    return "".join(c for c in (h or "").lower() if c.isalnum())[:60]


def _same_story(a, b):
    """
    Headline equality, tolerant of truncation and lost spaces.

    The model returned both "NASA invites ISRO to join ambitious Moon Base progra"
    and "join ambitious Moon Base programme" for one story; exact matching kept
    them as two articles.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 12 and (short in long_ or long_.startswith(short))


def _merge_by_headline(arts, by_n):
    """
    Fold entries that share a headline into one article.

    The model reliably identifies WHICH story a region belongs to -- it repeats
    that story's headline on each region -- but it emits them as separate
    entries instead of one grouped entry. Merging on the headline recovers the
    grouping it already worked out.
    """
    merged, order = {}, []
    for a in arts:
        # A layout label echoed into "type" means article, not a region kind.
        if a["type"] not in VALID_TYPES:
            a["type"] = "article"
        key = _norm_headline(a["headline_visible"])
        if not key:                       # unnamed: keep standalone
            order.append(a)
            continue
        hit = None
        for (mk, mt), cand in merged.items():
            if mt == a["type"] and _same_story(mk, key):
                hit = cand
                break
        if hit is None:
            merged[(key, a["type"])] = a
            order.append(a)
            continue
        m = hit
        m["elements"] = sorted(set(m["elements"]) | set(a["elements"]))
        b1, b2 = m["bbox_px"], a["bbox_px"]
        m["bbox_px"] = [min(b1[0], b2[0]), min(b1[1], b2[1]),
                        max(b1[2], b2[2]), max(b1[3], b2[3])]
        m["element_kinds"] = sorted({by_n[n]["kind"] for n in m["elements"]})
        m["has_photo"] = any(by_n[n]["kind"] == "figure" for n in m["elements"])
        m["package"] = m["package"] or a["package"]
        m["continued_on_page"] = m["continued_on_page"] or a["continued_on_page"]
    return [a for a in order if a in merged.values() or not _norm_headline(a["headline_visible"])]


def _ints(v):
    """Region ids may arrive as ints, "R7", or "7"."""
    out = []
    for x in (v or []):
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, str):
            d = "".join(c for c in x if c.isdigit())
            if d:
                out.append(int(d))
    return out


def _used_ids(data, valid):
    if not isinstance(data, dict):
        return set()
    return {n for a in data.get("articles", []) or []
            for n in _ints(a.get("regions")) if n in valid}


# -- stage 3: reading -----------------------------------------------------

READ_PROMPT = """This image is ONE article cut from a newspaper page.

Transcribe what is printed. Keep the original script and language. Do not
summarise, do not translate, do not invent text that is cut off at the edge.
Use null for any field that is not present.

The values below in <angle brackets> describe the TYPE of content expected.
They are placeholders. Never copy them into your answer.

Return ONLY JSON:
{"headline":"<headline exactly as printed>","subheadline":null,"kicker":null,
 "byline":null,"dateline":null,"location":null,
 "body":"<every paragraph of the body text, blank line between paragraphs>",
 "photo_captions":[],"continued_on_page":null,
 "transcription_confidence":0.0}"""

# The topic label used to be asked for here, in the same breath as the
# transcription. It is now epaper/classify.py's job, for two reasons. A model
# transcribing and judging at once does both worse -- the field was answered
# from the headline while the body was still being read. And the label was
# thrown away anyway: Corpus.ingest overwrote it with a beat id, so every
# transcription paid for an answer nothing ever read.


# What the model returns when it describes the field instead of filling it.
# The prompt now says not to, and GROUP_PROMPT has carried that warning all
# along, but a 7B model still does it occasionally and the result is worse
# than an empty body: it is fluent English that gets indexed, searched and
# shown to a reader as though it were the article. Observed once on a Jansatta
# page, stored in MongoDB, and only caught by eye.
_PLACEHOLDER_HINTS = (
    "paragraphs separated by blank lines",
    "blank line between paragraphs",
    "every paragraph of the body",
    "full body text",
    "headline exactly as printed",
    "headline text as printed",
    "headline as printed",
)

# Long enough that a real article quoting one of these phrases survives, short
# enough that a bare echo of the schema does not. A transcribed newspaper
# article is hundreds of characters; a placeholder is a few dozen.
_PLACEHOLDER_MAX = 120


def _real(text, field: str) -> str:
    """
    Drop a value that describes the field instead of filling it.

    Matched literally rather than by pattern, because the strings to catch are
    known exactly: they are the ones written into READ_PROMPT above. A pattern
    general enough to catch paraphrases would eventually eat a real sentence,
    and silently deleting an article is worse than keeping one bad one.
    """
    t = (text or "").strip()
    if not t:
        return ""
    bare = t.strip("<>").strip()
    low = bare.lower()
    echoed = (t.startswith("<") and t.endswith(">")) or (
        len(bare) <= _PLACEHOLDER_MAX
        and any(h in low for h in _PLACEHOLDER_HINTS))
    if echoed:
        print("transcribe: model echoed the %s placeholder, discarding %r"
              % (field, bare[:70]))
        return ""
    return t


def transcribe(crop_path: str) -> dict:
    """Read one article crop into structured fields."""
    out = vlm.chat_json(READ_PROMPT, crop_path, max_edge=1280, max_tokens=3000)
    if isinstance(out, dict):
        for field in ("headline", "body"):
            out[field] = _real(out.get(field), field)
    return out


# -- inspection artefacts -------------------------------------------------

def _annotate(page_path, boxes, colors, out_path, max_edge=1400):
    im = Image.open(page_path).convert("RGB")
    W = im.size[0]
    ov = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    lw = max(3, W // 420)
    for b, c in zip(boxes, colors):
        d.rectangle(b, fill=c + (28,), outline=c + (255,), width=lw)
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    im.thumbnail((max_edge, max_edge))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    im.save(out_path, "JPEG", quality=82)


def debug_dir_for(paper, date, page_no) -> str:
    safe = lambda s: "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))
    return os.path.join(DEBUG_DIR, safe(paper), safe(date), "p%s" % str(page_no).zfill(2))


# -- orchestration --------------------------------------------------------

def detect_regions(page_path: str, page_no: int = 1, paper: str = "", date: str = "",
                   debug: bool = True, lang: str | None = None) -> list:
    """
    Drop-in replacement for segment.detect_regions / autosegment.detect_regions.

    Writes elements.json, grouping.json and two annotated previews so the
    boundaries can be checked by eye before a full run.
    """
    elements = detect_elements(page_path)
    if not elements:
        raise LocalBackendError("DocLayout-YOLO found no elements on %s"
                                % os.path.basename(page_path))

    arts = group_articles(page_path, elements, lang=lang)

    with Image.open(page_path) as im:
        W, H = im.size
    for i, a in enumerate(arts, 1):
        x0, y0, x1, y1 = a["bbox_px"]
        a["id"] = i
        a["bbox"] = [round(x0 / W, 4), round(y0 / H, 4),
                     round(x1 / W, 4), round(y1 / H, 4)]
        a.setdefault("columns", None)

    if debug:
        dd = debug_dir_for(paper or "page", date or "", page_no)
        os.makedirs(dd, exist_ok=True)
        # encoding="utf-8" explicitly: open() would otherwise use the locale
        # encoding, and cp1252 cannot represent the Devanagari these files are
        # full of. Pairing that default with ensure_ascii=False raises rather
        # than writing mojibake.
        with open(os.path.join(dd, "elements.json"), "w", encoding="utf-8") as fh:
            json.dump({"page": page_path, "size": [W, H], "elements": elements},
                      fh, ensure_ascii=False, indent=1)
        with open(os.path.join(dd, "grouping.json"), "w", encoding="utf-8") as fh:
            json.dump({"page": page_path, "articles": arts},
                      fh, ensure_ascii=False, indent=1)
        _annotate(page_path,
                  [e["bbox_px"] for e in elements],
                  [ELEMENT_COLORS.get(e["kind"], (120, 120, 130)) for e in elements],
                  os.path.join(dd, "elements.jpg"))
        _annotate(page_path,
                  [a["bbox_px"] for a in arts],
                  [(34, 139, 230) if a["type"] == "article" else (240, 140, 40)
                   for a in arts],
                  os.path.join(dd, "articles.jpg"))
    return arts


def health() -> dict:
    """Report every prerequisite separately so failures are unambiguous."""
    if LAYOUT_URL:
        st = {"mode": "remote", "layout_url": LAYOUT_URL,
              "weights_path": LAYOUT_URL, "weights_present": False,
              "doclayout_yolo": True, "cuda": False, "gpu": None}
        try:
            with urllib.request.urlopen(LAYOUT_URL + "/health", timeout=20) as r:
                rem = json.loads(r.read().decode())
            st.update({"weights_present": bool(rem.get("weights_present")),
                       "cuda": bool(rem.get("cuda")), "gpu": rem.get("gpu"),
                       "torch": rem.get("torch"), "remote": rem})
        except Exception as ex:
            st["layout_error"] = "layout service unreachable at %s: %s" % (LAYOUT_URL, ex)
            st["doclayout_yolo"] = False
        st["vlm"] = vlm.health()
        st["ready"] = bool(st["weights_present"] and st["doclayout_yolo"]
                           and st["vlm"].get("available"))
        return st

    st = {"mode": "local", "weights_path": YOLO_W,
          "weights_present": os.path.exists(YOLO_W),
          "doclayout_yolo": False, "cuda": False, "gpu": None}
    try:
        import torch
        st["torch"] = torch.__version__
        st["cuda"] = bool(torch.cuda.is_available())
        if st["cuda"]:
            st["gpu"] = torch.cuda.get_device_name(0)
    except Exception as ex:
        st["torch_error"] = str(ex)[:200]
    try:
        import doclayout_yolo  # noqa: F401
        st["doclayout_yolo"] = True
    except Exception as ex:
        st["doclayout_error"] = str(ex)[:200]

    st["vlm"] = vlm.health()
    st["ready"] = bool(st["weights_present"] and st["doclayout_yolo"]
                       and st["vlm"].get("available"))
    return st
