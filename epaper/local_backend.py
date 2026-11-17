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

from . import geometry, ocr, vlm

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
    Prefer a GPU accelerator, but fall back to CPU instead of raising.

    Order: CUDA → Apple MPS → CPU. Downgrading the *detector* changes the
    answer and is banned; downgrading the *device* only changes how long the
    same answer takes. On a MacBook without CUDA, MPS is the difference
    between a few seconds and several minutes for DocLayout-YOLO.

    Force either way with EPAPER_DEVICE=cpu|cuda:0|mps.
    """
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda:0"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
        return "cpu"
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


# -- stage 2: grouping (Python + Shapely; see epaper.geometry) -------------

TITLE_KINDS = ("title",)
TEXTY = ("plain text", "plain_text", "title")


def _xov(a, b):
    """Horizontal overlap via Shapely (fraction of the narrower width)."""
    return geometry.horizontal_overlap_ratio(a, b)


def _dedupe_elements(elements: list) -> list:
    """
    Drop near-duplicate YOLO boxes (same region labelled title + plain_text).

    Prefer title/figure over plain_text when IoU is high so the lower headline
    is not stolen as body of the story above.
    """
    rank = {"title": 0, "figure": 1, "figure_caption": 2, "figure caption": 2,
            "plain text": 5, "plain_text": 5}
    ordered = sorted(
        elements,
        key=lambda e: (rank.get(e["kind"], 3),
                       -((e["bbox_px"][2] - e["bbox_px"][0]) *
                         (e["bbox_px"][3] - e["bbox_px"][1]))))
    kept = []
    for e in ordered:
        if any(geometry.iou(e["bbox_px"], k["bbox_px"]) >= 0.7 for k in kept):
            continue
        kept.append(e)
    kept.sort(key=lambda e: e["n"])
    return kept


def _blocked_by_intervening_title(anchor_bbox, block_bbox, titles, anchor_n) -> bool:
    """
    True if another title sits between anchor and block in the same column band.

    Uses the block's x-span (not only mutual overlap with the intervening
    title) so a wide headline cannot claim the next column's body past a
    narrower headline that only covers part of the width.
    """
    ay2 = anchor_bbox[3]
    by1 = block_bbox[1]
    if by1 <= ay2:
        return False
    bcx = (block_bbox[0] + block_bbox[2]) / 2.0
    for t in titles:
        if t["n"] == anchor_n:
            continue
        tb = t["bbox_px"]
        mid = (tb[1] + tb[3]) / 2.0
        if not (ay2 < mid < by1):
            continue
        # Same column as the block, or overlaps both anchor and block.
        covers_block = (tb[0] - 8) <= bcx <= (tb[2] + 8) or \
            geometry.same_column(tb, block_bbox) or \
            geometry.horizontal_overlap_ratio(tb, block_bbox) >= 0.20
        same_band = geometry.same_column(anchor_bbox, tb) or \
            geometry.horizontal_overlap_ratio(anchor_bbox, tb) >= 0.20
        if covers_block and same_band:
            return True
        # Even without anchor overlap: a title directly above the block wins.
        if covers_block and geometry.same_column(tb, block_bbox):
            return True
    return False


def _next_title_y(anchor_bbox, titles, anchor_n) -> float:
    """Top of the nearest later title that shares this column band."""
    ay2 = anchor_bbox[3]
    best = float("inf")
    for t in titles:
        if t["n"] == anchor_n:
            continue
        tb = t["bbox_px"]
        if tb[1] < ay2 - 4:
            continue
        if geometry.same_column(anchor_bbox, tb) or \
                geometry.horizontal_overlap_ratio(anchor_bbox, tb) >= 0.30:
            best = min(best, float(tb[1]))
    return best


def _clip_members_at_next_title(groups: list, titles: list, H: int) -> list:
    """Drop members that fall below a later title in the same column."""
    for g in groups:
        tb = g["title"]["bbox_px"]
        y_cap = _next_title_y(tb, titles, g["title"]["n"])
        kept = []
        for m in g["members"]:
            mb = m["bbox_px"]
            if not geometry.same_column(tb, mb):
                continue
            if _blocked_by_intervening_title(tb, mb, titles, g["title"]["n"]):
                continue
            # Hard stop at the next headline in this band — even if the member
            # sits in a sub-column the intervening title does not fully cover.
            if mb[1] >= y_cap - 2:
                continue
            # Hard cap: body more than ~15% of page below the headline is a
            # different story on dense broadsheets (Sakshi-style).
            dy = geometry.vertical_gap(tb, mb)
            is_fig = m["kind"] in ("figure", "figure_caption", "figure caption")
            if dy > (0.18 * H if is_fig else 0.12 * H):
                continue
            # Trim members that straddle the next title.
            if mb[3] > y_cap and y_cap < float("inf"):
                mb = list(mb)
                mb[3] = y_cap - 2
                if mb[3] - mb[1] < 12:
                    continue
                m = dict(m)
                m["bbox_px"] = mb
            kept.append(m)
        g["members"] = kept
    return groups


def _assign_to_titles(elements, W, H):
    """
    Assign every non-title region to the title that owns it.

    Titles are the right unit. Each headline owns exactly one story, and the
    ownership rule a typesetter uses is simple: a block belongs to the nearest
    headline ABOVE it whose horizontal span covers it. Overlap / gap math uses
    Shapely (epaper.geometry).
    """
    elements = _dedupe_elements(elements)
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
    # Never attach a box that is basically another title detection.
    title_boxes = [t["bbox_px"] for t in titles]
    others = [o for o in others
              if not any(geometry.iou(o["bbox_px"], tb) >= 0.55 for tb in title_boxes)]
    if not titles:
        return [], others

    groups = {t["n"]: {"title": t, "members": []} for t in titles}
    orphans = []
    for o in others:
        ob = o["bbox_px"]
        oh = max(1.0, ob[3] - ob[1])
        is_fig = o["kind"] in ("figure", "figure_caption", "figure caption")
        max_dy = 0.16 * H if is_fig else 0.10 * H
        # Nearest title ABOVE wins. Scoring by overlap alone let a wide
        # headline steal body that sits under a closer, narrower headline.
        best, best_key = None, None
        for t in titles:
            tb = t["bbox_px"]
            if not geometry.same_column(tb, ob):
                continue
            ov = geometry.horizontal_overlap_ratio(tb, ob)
            if ov < 0.22:
                continue
            dy = geometry.vertical_gap(tb, ob)  # block.top - title.bottom
            dy_above = tb[1] - ob[3]           # title.top - block.bottom
            # Plain text never attaches above the headline — that was a major
            # source of cross-story mega-crops on dense Telugu pages.
            above_fig = False
            if dy < -0.20 * oh:
                if not (is_fig and -0.15 * oh <= dy_above <= 0.10 * H):
                    continue
                above_fig = True
            if (not above_fig) and dy > max_dy:
                continue
            if (not above_fig) and _blocked_by_intervening_title(
                    tb, ob, titles, t["n"]):
                continue
            if above_fig:
                # Prefer the title closest below the figure.
                key = (1, max(0.0, dy_above), -ov)
            else:
                key = (0, max(0.0, dy), -ov)
            if best_key is None or key < best_key:
                best, best_key = t, key
        if best is not None:
            groups[best["n"]]["members"].append(o)
        else:
            orphans.append(o)
    out = _clip_members_at_next_title(list(groups.values()), titles, H)
    # Members dropped by the next-title clip become orphans so a lower
    # headline can claim them on a repair pass.
    claimed = {m["n"] for g in out for m in g["members"]}
    claimed |= {g["title"]["n"] for g in out}
    leftover = [o for o in others if o["n"] not in claimed]
    # Repair: assign leftover blocks to the nearest title above (strict).
    still = []
    for o in leftover:
        ob = o["bbox_px"]
        oh = max(1.0, ob[3] - ob[1])
        is_fig = o["kind"] in ("figure", "figure_caption", "figure caption")
        max_dy = 0.14 * H if is_fig else 0.10 * H
        best, best_key = None, None
        for g in out:
            t = g["title"]
            tb = t["bbox_px"]
            if not geometry.same_column(tb, ob):
                continue
            dy = geometry.vertical_gap(tb, ob)
            if dy < -0.05 * oh or dy > max_dy:
                continue
            if _blocked_by_intervening_title(tb, ob, titles, t["n"]):
                continue
            y_cap = _next_title_y(tb, titles, t["n"])
            if ob[1] >= y_cap - 2:
                continue
            key = (max(0.0, dy), -geometry.horizontal_overlap_ratio(tb, ob))
            if best_key is None or key < best_key:
                best, best_key = g, key
        if best is not None:
            best["members"].append(o)
        else:
            still.append(o)
    return out, still

HEADLINE_PROMPT = """This image is a headline cut from a newspaper page.

Transcribe the headline text exactly as printed, keeping the original script.
If the image is an advertisement rather than a news headline, set is_ad true.

Return ONLY JSON:
{"headline":"<text as printed>","is_ad":<true|false>}"""


def _read_headline(page_path, box, pad=20, lang=None):
    """
    Read one headline.

    Returns (text, is_ad, meta) where meta has backend/error for job diagnostics.
    Empty text on OCR failure is accompanied by meta["error"] — callers must
    not treat silence as a successful blank headline.
    """
    from PIL import Image as _I
    with _I.open(page_path) as im:
        x0, y0, x1, y1 = box
        crop = im.convert("RGB").crop((max(0, x0 - pad), max(0, y0 - pad),
                                       min(im.width, x1 + pad),
                                       min(im.height, y1 + pad)))

    meta = {"backend": None, "error": "", "empty": True}
    # auto: IndicOCR first, VLM if empty/unreliable (fewer blank headlines).
    mode = os.environ.get("EPAPER_HEADLINE", "auto").lower()
    if mode in ("ocr", "auto"):
        detail = ocr.read_headline_detail(crop, lang=lang)
        meta = {k: detail.get(k) for k in ("backend", "error", "empty")}
        text = detail.get("text") or ""
        if mode == "ocr":
            return text, False, meta
        if text and not ocr.looks_unreliable(text):
            return text, False, meta

    try:
        d = vlm.chat_json(HEADLINE_PROMPT, crop, max_edge=900, max_tokens=300)
        text = (d.get("headline") or "").strip()
        meta = {"backend": "vlm", "error": "", "empty": not bool(text)}
        return text, bool(d.get("is_ad")), meta
    except Exception as ex:
        meta = {"backend": "vlm", "error": str(ex)[:200], "empty": True}
        return "", False, meta


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
    figs = [m["bbox_px"] for m in members if m["kind"] == "figure"]
    return geometry.figure_area_ratio(figs, box)


def _pair_captions(members: list) -> list:
    """
    Tag figure_caption members with the nearest figure id (diagnostic only).
    Does not reassign ownership — captions already belong to a title group.
    Distance uses Shapely centroids.
    """
    figs = [m for m in members if m["kind"] == "figure"]
    if not figs:
        return members
    out = []
    for m in members:
        if m["kind"] not in ("figure_caption", "figure caption"):
            out.append(m)
            continue
        cb = m["bbox_px"]
        best, best_d = None, 1e18
        for f in figs:
            fb = f["bbox_px"]
            d = geometry.center_distance2(cb, fb)
            # Prefer captions below the figure; penalise captions far above.
            dy = geometry.vertical_gap(fb, cb)
            if dy < -abs(fb[3] - fb[1]):
                d *= 3.0
            if d < best_d:
                best, best_d = f, d
        mm = dict(m)
        if best is not None:
            mm["caption_for"] = best["n"]
        out.append(mm)
    return out

def _attach_orphans(groups: list, orphans: list, W: int, H: int):
    """
    Conservative second pass for leftovers.

    - Figures: only tight, same-column hero photos above/below a headline.
    - Plain text: only a short deck/standfirst sitting immediately ABOVE its
      headline (common on Indian fronts). Never reach across the page.
    """
    if not groups or not orphans:
        return groups, orphans
    titles = [g["title"] for g in groups]
    still = []
    for o in orphans:
        ob = o["bbox_px"]
        oh = max(1.0, ob[3] - ob[1])
        is_fig = o["kind"] in ("figure", "figure_caption", "figure caption")
        is_text = o["kind"] in ("plain text", "plain_text")
        if not (is_fig or is_text):
            still.append(o)
            continue
        best, best_score = None, -1e9
        for g in groups:
            tb = g["title"]["bbox_px"]
            if not geometry.same_column(tb, ob):
                continue
            ov = geometry.horizontal_overlap_ratio(tb, ob)
            if ov < 0.28:
                continue
            dy_below = geometry.vertical_gap(tb, ob)
            dy_above = tb[1] - ob[3]
            score = None
            if is_fig:
                if -0.2 * oh <= dy_below <= 0.08 * H:
                    if _blocked_by_intervening_title(tb, ob, titles, g["title"]["n"]):
                        continue
                    score = ov * 100.0 - abs(max(0.0, dy_below)) / float(H) * 100.0
                elif -0.15 * oh <= dy_above <= 0.08 * H:
                    score = ov * 100.0 - abs(max(0.0, dy_above)) / float(H) * 100.0 + 5.0
            else:
                # Deck immediately above headline only.
                if 0.0 <= dy_above <= 0.05 * H and oh < 0.12 * H:
                    score = ov * 100.0 - dy_above / float(H) * 120.0
            if score is not None and score > best_score:
                best, best_score = g, score
        if best is not None and best_score > 25.0:
            best["members"].append(o)
        else:
            still.append(o)
    return groups, still


def group_articles(page_path: str, elements: list, lang=None) -> list:
    """Title-anchored grouping: geometry proposes, OCR/VLM reads each headline."""
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
    groups, orphans = _attach_orphans(groups, orphans, W, H)
    if not groups:
        raise LocalBackendError("no titles detected on %s; cannot anchor articles"
                                % os.path.basename(page_path))

    arts = []
    for g in groups:
        t = g["title"]
        members = _pair_captions(g["members"])
        # Clip members to the title column before union so a partially
        # overlapping banner figure cannot inflate the crop across the page.
        clipped_boxes = [t["bbox_px"]]
        clipped_members = []
        for m in members:
            cb = geometry.clip_box_to_column(t["bbox_px"], m["bbox_px"])
            if cb is None:
                continue
            mm = dict(m)
            mm["bbox_px"] = cb
            clipped_members.append(mm)
            clipped_boxes.append(cb)
        members = clipped_members
        box = geometry.union_bbox(clipped_boxes)
        # Guard: never let one story span > ~55% page width unless the title
        # itself is that wide (banner headline).
        tw = max(1.0, t["bbox_px"][2] - t["bbox_px"][0])
        if (box[2] - box[0]) > max(1.35 * tw, 0.55 * W) and tw < 0.45 * W:
            pad = max(48.0, 0.55 * tw)
            box = [max(box[0], t["bbox_px"][0] - pad), box[1],
                   min(box[2], t["bbox_px"][2] + pad), box[3]]
        # Clip bottom at the next headline in this column band.
        y_cap = _next_title_y(t["bbox_px"], [g["title"] for g in groups], t["n"])
        if y_cap < float("inf") and box[3] > y_cap:
            box = [box[0], box[1], box[2], max(box[1] + 8, y_cap - 2)]
        headline, is_ad, ocr_meta = texts.get(t["n"], ("", False, {}))
        kinds = sorted({m["kind"] for m in members} | {t["kind"]})
        figr = _figure_ratio(members, box)
        # Ads: model flag, or almost-all-figure with little/no headline text.
        kind = "ad" if (is_ad or (figr > 0.70 and len(headline) < 8)) else "article"
        # Masthead: top band, wide, few body paragraphs.
        plainish = sum(1 for m in members
                       if m["kind"] in ("plain text", "plain_text"))
        if (box[1] < H * 0.08 and (box[2] - box[0]) > W * 0.50
                and plainish <= 1 and (box[3] - box[1]) < 0.22 * H):
            kind = "masthead"
        # Strip leaked JSON fragments from OCR/VLM headline quirks.
        if headline and ("is_ad" in headline or '"is_ad"' in headline
                         or headline.strip().startswith("{")):
            for sep in (",'", '","', '",', ", \""):
                if sep in headline:
                    headline = headline.split(sep)[0]
                    break
            headline = headline.strip(" {}\\\"'").strip()
        # Record geometry engine for diagnostics / health reports.
        geom_engine = "shapely"
        status = "ok"
        if kind == "article" and not headline and ocr_meta.get("error"):
            status = "partial"
        arts.append({
            "type": kind,
            "headline_visible": headline,
            "headline_ocr": headline,
            "ocr_backend": ocr_meta.get("backend"),
            "ocr_error": ocr_meta.get("error") or None,
            "extraction_status": status,
            "continued_on_page": None,
            "package": False,
            # Per-element boxes, kept rather than collapsed into the article
            # bbox. The union box is enough to crop, but not enough to point a
            # reader at the headline inside it, and this is the only place the
            # member geometry still exists. `text` is filled for the headline
            # only -- body text per region needs OCR, which does not run here.
            "region_boxes": (
                [{"type": "headline", "bbox": list(t["bbox_px"]), "text": headline}]
                + [{"type": m["kind"], "bbox": list(m["bbox_px"]), "text": "",
                    **({"caption_for": m["caption_for"]}
                       if m.get("caption_for") is not None else {})}
                   for m in members]),
            "elements": sorted([t["n"]] + [m["n"] for m in members]),
            "element_kinds": kinds,
            "has_photo": any(m["kind"] == "figure" for m in members),
            "grouping_confidence": round(min(1.0, 0.4 + 0.1 * len(members)), 2),
            "figure_ratio": round(figr, 2),
            "bbox_px": box,
            "geometry": geom_engine,
            "detector": "doclayout-yolo(titles)+shapely+%s" % vlm.MODEL,
        })

    # Orphaned blocks — no headline owned them. Emitting every leftover as an
    # empty article created duplicate/junk cards on dense pages. Only keep
    # compact figure orphans that look like standalone ads/photos.
    covered = [a["bbox_px"] for a in arts]
    for o in orphans:
        if o["kind"] not in ("figure",):
            continue
        b = o["bbox_px"]
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bw < W * 0.06 or bh < H * 0.02:
            continue
        # Page-wide top banners belong with the masthead, not as a lone ad crop.
        if bw > 0.55 * W or (b[1] < 0.08 * H and bw > 0.40 * W):
            continue
        if any(geometry.iou(b, cb) >= 0.5 or (
                cb[0] <= (b[0] + b[2]) / 2 <= cb[2]
                and cb[1] <= (b[1] + b[3]) / 2 <= cb[3]) for cb in covered):
            continue
        arts.append({
            "type": "ad",
            "headline_visible": "", "continued_on_page": None, "package": False,
            "elements": [o["n"]], "element_kinds": [o["kind"]],
            "has_photo": True, "grouping_confidence": 0.2,
            "figure_ratio": 1.0,
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
    them as two articles. Short OCR collisions (≤10 chars) are NOT enough on
    their own — callers must also check spatial proximity.
    """
    if not a or not b:
        return False
    if a == b:
        return len(a) >= 8
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= 14 and (short in long_ or long_.startswith(short))


def _boxes_mergeable(a, b) -> bool:
    """Same-story merge only when boxes share a column and are not far apart."""
    if not (geometry.same_column(a, b) or
            geometry.horizontal_overlap_ratio(a, b) >= 0.30):
        return False
    if a[3] < b[1]:
        gap = b[1] - a[3]
    elif b[3] < a[1]:
        gap = a[1] - b[3]
    else:
        gap = 0.0
    # Distinct columns that OCR to the same short string are often far apart
    # vertically; refuse to weld them into one crop.
    if gap > 400:
        return False
    return True


def _merge_by_headline(arts, by_n):
    """
    Fold entries that share a headline into one article.

    The model reliably identifies WHICH story a region belongs to -- it repeats
    that story's headline on each region -- but it emits them as separate
    entries instead of one grouped entry. Merging on the headline recovers the
    grouping it already worked out. Spatial checks stop short OCR collisions
    from welding unrelated columns into a page-tall mega-crop.
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
            if mt == a["type"] and _same_story(mk, key) and \
                    _boxes_mergeable(cand["bbox_px"], a["bbox_px"]):
                hit = cand
                break
        if hit is None:
            merged[(key, a["type"])] = a
            order.append(a)
            continue
        m = hit
        m["elements"] = sorted(set(m["elements"]) | set(a["elements"]))
        m["bbox_px"] = geometry.union_bbox([m["bbox_px"], a["bbox_px"]])
        # Keep region_boxes in sync so the UI overlay matches the crop.
        if m.get("region_boxes") is not None and a.get("region_boxes"):
            m["region_boxes"] = list(m["region_boxes"]) + list(a["region_boxes"])
        m["element_kinds"] = sorted({by_n[n]["kind"] for n in m["elements"]
                                      if n in by_n} or set(m.get("element_kinds") or []))
        m["has_photo"] = m.get("has_photo") or a.get("has_photo") or \
            any(by_n.get(n, {}).get("kind") == "figure" for n in m["elements"])
        m["package"] = m["package"] or a["package"]
        m["continued_on_page"] = m["continued_on_page"] or a["continued_on_page"]
    return [a for a in order if a in merged.values() or not _norm_headline(a["headline_visible"])]


# -- stage 3: reading (Qwen2.5-VL) ----------------------------------------

READ_PROMPT = """This image is ONE article cut from a newspaper page.

Transcribe what is printed. Keep the original script and language. Do not
translate. Do not invent names, places, dates or facts that are not visible.
Use null or [] for any field that is not present on the page.

The values below in <angle brackets> describe the TYPE of content expected.
They are placeholders. Never copy them into your answer.

Return ONLY JSON:
{"headline":"<headline exactly as printed>","subheadline":null,"kicker":null,
 "byline":null,"dateline":null,"location":null,
 "body":"<every paragraph of the body text, blank line between paragraphs>",
 "summary":null,
 "photo_captions":[],"continued_on_page":null,
 "keywords":[],"people":[],"organizations":[],"events":[],"places":[],
 "topics":[],
 "transcription_confidence":0.0}"""

# The topic label used to be asked for here, in the same breath as the
# transcription. It is now epaper/classify.py's job, for two reasons. A model
# transcribing and judging at once does both worse -- the field was answered
# from the headline while the body was still being read. And the label was
# thrown away anyway: Corpus.ingest overwrote it with a beat id, so every
# transcription paid for an answer nothing ever read.


# What the model returns when it describes the field instead of filling it.
# A 7B model still echoes schema placeholders occasionally; catching them
# avoids indexing fluent English that is not the article.
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
    """Read one article crop into structured fields (validated when possible)."""
    # 4500: Telugu front pages often truncate at 3000 mid-JSON ("subheadline": nul).
    out = vlm.chat_json(READ_PROMPT, crop_path, max_edge=1280, max_tokens=4500)
    if isinstance(out, dict):
        for field in ("headline", "body", "summary"):
            if field in out:
                out[field] = _real(out.get(field), field)
        try:
            from .schemas import validate_vlm_dict
            validated = validate_vlm_dict(out)
            data = validated.model_dump()
            # Keep raw model payload for debugging; do not lose unmapped keys.
            data["raw"] = out
            return data
        except Exception:
            out.setdefault("raw", dict(out))
            return out
    return {"headline": "", "body": "", "raw": out, "transcription_error": "non-dict"}


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
    DocLayout-YOLO + Shapely grouping for one page.

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
        st["ocr"] = ocr.health()
        st["ready"] = bool(st["weights_present"] and st["doclayout_yolo"]
                           and st["vlm"].get("available"))
        return st

    st = {"mode": "local", "weights_path": YOLO_W,
          "weights_present": os.path.exists(YOLO_W),
          "doclayout_yolo": False, "cuda": False, "gpu": None,
          "device": _default_device()}
    try:
        import torch
        st["torch"] = torch.__version__
        st["cuda"] = bool(torch.cuda.is_available())
        mps = getattr(torch.backends, "mps", None)
        st["mps"] = bool(mps is not None and mps.is_available())
        if st["cuda"]:
            st["gpu"] = torch.cuda.get_device_name(0)
        elif st["mps"]:
            st["gpu"] = "Apple MPS"
        forced = (os.environ.get("EPAPER_DEVICE") or "").strip()
        if forced:
            st["device"] = forced
    except Exception as ex:
        st["torch_error"] = str(ex)[:200]
    try:
        import doclayout_yolo  # noqa: F401
        st["doclayout_yolo"] = True
    except Exception as ex:
        st["doclayout_error"] = str(ex)[:200]

    st["vlm"] = vlm.health()
    st["ocr"] = ocr.health()
    st["ready"] = bool(st["weights_present"] and st["doclayout_yolo"]
                       and st["vlm"].get("available"))
    return st
