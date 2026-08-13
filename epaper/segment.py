"""
Article segmentation for pages with no publisher metadata.

This is the stage that fixes mismatched content. The mistake that produces
"headline of article A + body of article B" is running OCR across a whole page
and reading the text back in raster order. A newspaper is not one column of
text, so raster order is wrong by construction.

Two passes, deliberately separated:

  Pass 1 - LAYOUT. The page is sent whole, downscaled, and the model is asked
           only WHERE each article sits: bounding boxes plus which box is
           headline / body / photo / caption. It is explicitly not asked to
           transcribe. Downscaling is fine here because article boundaries are
           visible from gutters, rules and whitespace, not from reading words.

  Pass 2 - TEXT. Each article box from pass 1 is cropped out of the FULL
           resolution page and sent on its own. The model now sees one article
           filling the frame, so 7pt body text is legible and there is no
           neighbouring column to bleed in.

Keeping these apart is the whole point. A single call that asks for boxes and
text together degrades both: the model spends its attention transcribing and
gets the boundaries wrong, which is precisely the failure being fixed.

Requires ANTHROPIC_API_KEY. Model is configurable; Opus 5 is the default
because layout reasoning over a dense broadsheet is where the accuracy gap
between tiers actually shows up.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.request

from PIL import Image

MODEL = os.environ.get("EPAPER_MODEL", "claude-opus-5")
API_URL = "https://api.anthropic.com/v1/messages"

# Claude downsizes images past ~1568px on the long edge, so sending a 3700px
# page at full size wastes tokens and buys nothing for the layout pass.
LAYOUT_MAX_EDGE = 1568

LAYOUT_PROMPT = """You are looking at one full page of a printed newspaper.

Identify every distinct EDITORIAL ARTICLE on the page. An article is one news
item: its headline plus the body text that belongs to it, plus any photo and
caption that belong to it.

Rules:
- Articles often span several columns. Group all columns of one article together.
- A photo belongs to the article whose text wraps or sits adjacent to it.
- Treat display advertisements as type "ad", not as articles.
- Treat the masthead / nameplate strip as type "masthead".
- Include short single-column briefs as their own articles.
- If text says "CONTINUED ON PAGE n", record that page number.

Return coordinates as fractions of page width and height, 0.0 to 1.0, where
[0,0] is top-left. Be generous: the box must contain the whole article.

Return ONLY valid JSON, no prose:
{"regions":[{"id":1,"type":"article|ad|masthead|index",
  "headline_visible":"the headline as printed, best effort",
  "bbox":[x0,y0,x1,y1],
  "columns":<int>,"has_photo":true|false,
  "continued_on_page":<int or null>}]}"""

TEXT_PROMPT = """This image is ONE article cut from a newspaper page.

Transcribe it into structured fields. Transcribe only what is printed in this
image. Do not summarise, do not translate, do not invent or complete text that
is cut off. If a field is not present, use null.

Return ONLY valid JSON, no prose:
{"headline":"...","subheadline":null,"kicker":null,
 "byline":null,"dateline":null,
 "body":"full body text, paragraphs separated by blank lines",
 "photo_captions":[],"continued_on_page":null,
 "category":"one of: politics, crime, business, sports, civic, health, education, world, entertainment, other",
 "transcription_confidence":0.0}"""


class NoApiKey(RuntimeError):
    pass


def _key() -> str:
    k = os.environ.get("ANTHROPIC_API_KEY")
    if not k:
        raise NoApiKey(
            "ANTHROPIC_API_KEY is not set. The layout and text passes both "
            "need it. export ANTHROPIC_API_KEY=sk-ant-... and re-run.")
    return k


def _encode(img: Image.Image, max_edge: int = None) -> str:
    if max_edge and max(img.size) > max_edge:
        r = max_edge / float(max(img.size))
        img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _call(prompt: str, img_b64: str, max_tokens: int = 8000) -> str:
    body = json.dumps({
        "model": MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/jpeg",
                                         "data": img_b64}},
            {"type": "text", "text": prompt},
        ]}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, headers={
        "x-api-key": _key(),
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read().decode())
    return "".join(b.get("text", "") for b in data.get("content", []))


def _parse_json(text: str):
    """Models occasionally wrap JSON in a fence; tolerate it."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"[\{\[].*[\}\]]", text, re.S)
        return json.loads(m.group(0)) if m else None


# -- pass 1 ---------------------------------------------------------------

def detect_regions(page_path: str) -> list:
    """Locate articles on a page. Returns regions with pixel bboxes attached."""
    with Image.open(page_path) as im:
        W, H = im.size
        b64 = _encode(im, LAYOUT_MAX_EDGE)
    out = _parse_json(_call(LAYOUT_PROMPT, b64)) or {}
    regions = out.get("regions", [])
    for r in regions:
        x0, y0, x1, y1 = r.get("bbox", [0, 0, 0, 0])
        r["bbox_px"] = [int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)]
    return regions


# -- pass 2 ---------------------------------------------------------------

def crop_region(page_path: str, bbox_px: list, dest: str, pad: int = 12) -> str:
    """Cut an article out of the full-resolution page."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with Image.open(page_path) as im:
        x0, y0, x1, y1 = bbox_px
        box = (max(0, x0 - pad), max(0, y0 - pad),
               min(im.width, x1 + pad), min(im.height, y1 + pad))
        im.crop(box).save(dest, "JPEG", quality=94)
    return dest


def transcribe(crop_path: str) -> dict:
    """Read one article crop into structured fields."""
    with Image.open(crop_path) as im:
        b64 = _encode(im)          # full resolution -- do not downscale here
    return _parse_json(_call(TEXT_PROMPT, b64)) or {}


# -- orchestration --------------------------------------------------------

def segment_page(page_path: str, crops_dir: str, page_no=1) -> list:
    """Full two-pass extraction for one page. Returns article dicts."""
    regions = detect_regions(page_path)
    articles = []
    seq = 0
    for r in regions:
        if r.get("type") != "article":
            continue
        seq += 1
        crop = os.path.join(crops_dir, "p%s_a%02d.jpg" % (str(page_no).zfill(2), seq))
        crop_region(page_path, r["bbox_px"], crop)
        fields = transcribe(crop)
        articles.append({
            "seq": seq,
            "bbox": r.get("bbox"),
            "bbox_px": r["bbox_px"],
            "columns": r.get("columns"),
            "has_photo": r.get("has_photo"),
            "crop_image": crop,
            "text_source": "vlm",
            **fields,
        })
    return articles


def score(article: dict) -> float:
    """
    Cheap sanity score so bad extractions can be quarantined rather than
    silently published. Deliberately conservative.
    """
    s, body = 1.0, (article.get("body") or "")
    if not article.get("headline"):
        s -= 0.35
    if len(body) < 120:
        s -= 0.25
    if article.get("transcription_confidence") is not None:
        s = min(s, 0.35 + 0.65 * float(article["transcription_confidence"]))
    # A headline repeated verbatim inside the body usually means two articles
    # were merged into one box.
    h = (article.get("headline") or "").strip()
    if h and body.count(h) > 1:
        s -= 0.2
    return round(max(0.0, min(1.0, s)), 2)
