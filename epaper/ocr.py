"""
Headline OCR: the 72% saving.

Profiling a real Tribune page put 344 of 475 seconds inside one stage, and it
turned out not to be where the README's argument against OCR applies. The
grouping geometry is already deterministic (`_assign_to_titles`); what cost the
time was `_read_headline` sending *every title candidate* to the vision model --
18 crops on that page, ~19 s each.

The README's objection to OCR is that it reads a page in raster order, which is
wrong for a newspaper by construction. That objection does not apply here. By
this point YOLO has already isolated a single headline box, so there is no
reading order left to get wrong -- only a strip of large bold type to
recognise. The README says as much: "OCR the headline strip off the article
crop -- headlines are large and bold, so recognition there is near perfect."

What is lost, and it is worth stating plainly: the VLM prompt also returned an
`is_ad` flag, and OCR cannot. Ad detection therefore falls back to the existing
geometric test (`figure_ratio > 0.65`) alone, which is weaker. Set
EPAPER_HEADLINE=vlm to restore the old behaviour if that matters more than the
five minutes a page.

Tesseract rather than a neural OCR because the input is the easy case -- big,
high-contrast, already-cropped type -- and it costs milliseconds on CPU instead
of competing with the VLM for a 4GB card.

Environment:
    EPAPER_HEADLINE   ocr (default) | vlm | auto
    EPAPER_TESSERACT  path to tesseract.exe, if not on PATH
    EPAPER_OCR_LANGS  tesseract language string, default "eng+hin"
"""

from __future__ import annotations

import os
import re
import shutil

MODE = os.environ.get("EPAPER_HEADLINE", "ocr").lower()
LANGS = os.environ.get("EPAPER_OCR_LANGS", "eng+hin")

_WIN_DEFAULTS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"),
)

_state = {"checked": False, "ok": False, "error": "", "langs": [], "exe": ""}


def _find_exe():
    p = os.environ.get("EPAPER_TESSERACT")
    if p and os.path.exists(p):
        return p
    p = shutil.which("tesseract")
    if p:
        return p
    for cand in _WIN_DEFAULTS:
        if os.path.exists(cand):
            return cand
    return ""


def available() -> dict:
    """Probe once. Reports which requested languages are actually installed."""
    if _state["checked"]:
        return dict(_state)
    _state["checked"] = True
    exe = _find_exe()
    if not exe:
        _state["error"] = ("tesseract not found. Install it, or set "
                           "EPAPER_TESSERACT to tesseract.exe")
        return dict(_state)
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = exe
        langs = list(pytesseract.get_languages(config=""))
        _state.update(exe=exe, langs=langs, ok=True)
        missing = [l for l in LANGS.split("+") if l not in langs]
        if missing:
            # Not fatal: drop the missing ones rather than fail every call.
            # A Devanagari headline read with eng only comes back as noise,
            # which the caller can see; refusing to read anything is worse.
            _state["error"] = ("language data missing: %s (have %s)"
                               % (", ".join(missing), ", ".join(langs[:8])))
    except Exception as ex:
        _state["error"] = str(ex)[:200]
    return dict(_state)


def langs_available() -> str:
    st = available()
    if not st["ok"]:
        return LANGS
    have = [l for l in LANGS.split("+") if l in st["langs"]]
    return "+".join(have) or "eng"


_JUNK = re.compile(r"[|_~^`]+")


def clean(text: str) -> str:
    """
    Tidy a recognised headline without inventing content.

    Tesseract emits per-line output with hard breaks where the headline wrapped
    across the crop; a headline is one phrase, so those are joined. Stray
    pipe/underscore glyphs come from column rules clipped into the crop.
    """
    t = _JUNK.sub(" ", text or "")
    t = " ".join(t.split())
    return t.strip(" -–—·.,")


def read_headline(image, psm: int = 7) -> str:
    """
    Recognise one already-cropped headline. Returns "" if OCR is unavailable.

    psm 7 = "treat the image as a single text line", which is what a headline
    box usually is. Falls back to psm 6 (a uniform block) when 7 yields
    nothing, because two- and three-line headlines are common.
    """
    st = available()
    if not st["ok"]:
        return ""
    import pytesseract
    cfg = "--psm %d" % psm
    try:
        txt = pytesseract.image_to_string(image, lang=langs_available(), config=cfg)
        out = clean(txt)
        if len(out) < 3 and psm == 7:
            out = clean(pytesseract.image_to_string(
                image, lang=langs_available(), config="--psm 6"))
        return out
    except Exception:
        return ""


def looks_unreliable(text: str) -> bool:
    """
    Does this recognition look wrong enough to be worth a model call?

    Measured against 18 real Tribune headlines. Tesseract handled story
    headlines well and failed on two specific things: ornate mastheads and
    all-caps bylines, which came back as "ae thei", "Ap1t1 TANDON",
    "Saunane MAL".

    The failure that actually matters is not a typo -- "tum" for "turn" still
    matches nothing important -- it is COLLAPSED SPACING. keywords.py matches
    Latin terms on whole-word boundaries, so a headline read as
    "policearrested" scores zero and the article drops out of the beat
    entirely. A wrong word is survivable; an invisible article is not.

    Deliberately cheap and conservative: a false alarm costs one model call,
    a miss costs a lost story.
    """
    t = (text or "").strip()
    if len(t) < 8:
        return True
    words = t.split()
    letters = sum(c.isalpha() for c in t)
    if letters < len(t) * 0.55:                 # digits/symbols dominate
        return True
    if any(len(w) > 20 for w in words):         # spacing collapsed
        return True
    for w in words:                             # "Ap1t1", "Retestof3UGC"
        if re.search(r"[^\W\d_]\d|\d[^\W\d_]", w):
            return True
    return False


def health() -> dict:
    st = available()
    return {"backend": "tesseract", "mode": MODE, "available": st["ok"],
            "exe": st["exe"], "langs_requested": LANGS,
            "langs_used": langs_available(),
            "langs_installed": st["langs"][:20], "error": st["error"]}
