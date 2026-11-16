"""
Vision-model provider abstraction.

The pipeline must not care which server is answering. Ollama is already running
on the GPU box and is usable today; vLLM is blocked behind an NVML driver
mismatch and will become available later. Both speak a different HTTP dialect,
so both are wrapped behind one call:

    vlm.chat_json(prompt, image) -> parsed dict

Select with EPAPER_VLM_BACKEND=ollama|vllm. Nothing else in the pipeline
changes when that flips.

Environment:
    EPAPER_VLM_BACKEND   ollama (default) | vllm
    EPAPER_VLM           model name/tag
    EPAPER_OLLAMA        http://localhost:11434
    EPAPER_VLLM          http://localhost:8000/v1
    EPAPER_VLLM_API_KEY  optional Bearer token for the OpenAI-compatible endpoint
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.error
import urllib.request

from PIL import Image

from . import config as _config

_config.load_env()

BACKEND = os.environ.get("EPAPER_VLM_BACKEND", "ollama").lower()
OLLAMA = os.environ.get("EPAPER_OLLAMA", "http://localhost:11434").rstrip("/")
# Ollama's default context is 4096 tokens. A full-page image alone can exceed
# that, and Ollama truncates silently rather than erroring -- the model then
# answers about a page it only half saw. Set it explicitly.
NUM_CTX = int(os.environ.get("EPAPER_NUM_CTX", "16384"))
VLLM = os.environ.get("EPAPER_VLLM", "http://localhost:8000/v1").rstrip("/")
VLLM_API_KEY = (os.environ.get("EPAPER_VLLM_API_KEY")
                or os.environ.get("LLM_API_KEY") or "").strip()

_DEFAULT_MODEL = {"ollama": "qwen2.5vl:7b", "vllm": "Qwen/Qwen2.5-VL-7B-Instruct-AWQ"}
MODEL = os.environ.get("EPAPER_VLM", _DEFAULT_MODEL.get(BACKEND, "qwen2.5vl:7b"))

# Tags that can actually read an image. A text-only model answering a vision
# prompt returns confident nonsense, so this is checked rather than assumed.
VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "gemma3", "moondream",
                "pixtral", "internvl", "bakllava")


# Text-only reasoning (topic screening, relevance) reuses the VISION model by
# default. A T4 holds one 7B model comfortably but not two: loading a separate
# text model pushed it to 15%/85% CPU/GPU and screening 36 articles stalled for
# minutes. A VL model answers text-only prompts perfectly well, so sharing the
# resident one is both faster and cheaper. Override only if VRAM allows.
TEXT_MODEL = os.environ.get("EPAPER_TEXT_MODEL", "") or os.environ.get(
    "EPAPER_VLM", "qwen2.5vl:7b")


class VLMError(RuntimeError):
    pass


def encode(image, max_edge: int = 1400) -> str:
    """Accept a path or a PIL image; return base64 JPEG."""
    im = Image.open(image) if isinstance(image, str) else image
    try:
        if max(im.size) > max_edge:
            r = max_edge / float(max(im.size))
            im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))),
                           Image.LANCZOS)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=90)
        return base64.b64encode(buf.getvalue()).decode()
    finally:
        if isinstance(image, str):
            im.close()


def _headers(json_body: bool = True) -> dict:
    h = {}
    if json_body:
        h["Content-Type"] = "application/json"
    if BACKEND == "vllm" and VLLM_API_KEY:
        h["Authorization"] = "Bearer " + VLLM_API_KEY
    return h


def _post(url: str, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as ex:
        raise VLMError("%s returned HTTP %s: %s"
                       % (url, ex.code, ex.read()[:300].decode("utf-8", "ignore")))
    except Exception as ex:
        raise VLMError("%s unreachable: %s" % (url, ex))


def chat(prompt: str, img_b64: str, max_tokens: int = 4000, timeout: int = 600) -> str:
    """Raw text response from whichever backend is selected."""
    if BACKEND == "ollama":
        out = _post(OLLAMA + "/api/chat", {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
            "stream": False,
            "format": "json",          # Ollama constrains output to valid JSON
            "options": {"temperature": 0, "num_predict": max_tokens,
                        "num_ctx": NUM_CTX},
        }, timeout)
        return (out.get("message") or {}).get("content", "")

    if BACKEND == "vllm":
        out = _post(VLLM + "/chat/completions", {
            "model": MODEL, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64," + img_b64}},
                {"type": "text", "text": prompt}]}],
        }, timeout)
        return out["choices"][0]["message"]["content"]

    raise VLMError("unknown EPAPER_VLM_BACKEND=%r (use ollama or vllm)" % BACKEND)


def chat_text(prompt: str, max_tokens: int = 2000, timeout: int = 300) -> str:
    """Text-only completion, no image."""
    if BACKEND == "ollama":
        out = _post(OLLAMA + "/api/chat", {
            "model": TEXT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False, "format": "json",
            "options": {"temperature": 0, "num_predict": max_tokens,
                        "num_ctx": NUM_CTX},
        }, timeout)
        return (out.get("message") or {}).get("content", "")
    out = _post(VLLM + "/chat/completions", {
        "model": TEXT_MODEL, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }, timeout)
    return out["choices"][0]["message"]["content"]


def text_json(prompt: str, max_tokens: int = 2000, timeout: int = 300):
    return parse_json(chat_text(prompt, max_tokens, timeout))


def parse_json(text: str):
    """Tolerate fences and leading prose around the JSON body."""
    if not text or not text.strip():
        raise VLMError("model returned an empty response")
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"[\{\[].*[\}\]]", t, re.S)
        if not m:
            raise VLMError("model did not return JSON: %r" % text[:300])
        try:
            return json.loads(m.group(0))
        except Exception as ex:
            raise VLMError("model returned malformed JSON (%s): %r" % (ex, text[:300]))


def chat_json(prompt: str, image, max_edge: int = 1400,
              max_tokens: int = 4000, timeout: int = 600):
    """The one call the pipeline uses."""
    return parse_json(chat(prompt, encode(image, max_edge), max_tokens, timeout))


# -- health ---------------------------------------------------------------

def _ollama_tags():
    req = urllib.request.Request(OLLAMA + "/api/tags")
    with urllib.request.urlopen(req, timeout=15) as r:
        return [m["name"] for m in json.loads(r.read().decode()).get("models", [])]


def _vllm_models():
    req = urllib.request.Request(VLLM + "/models", headers=_headers(json_body=False))
    with urllib.request.urlopen(req, timeout=15) as r:
        return [m["id"] for m in json.loads(r.read().decode())["data"]]


def health() -> dict:
    """Is the selected backend up, and is the chosen model vision-capable?"""
    st = {"backend": BACKEND, "model": MODEL, "available": False,
          "num_ctx": NUM_CTX if BACKEND == "ollama" else None,
          "endpoint": OLLAMA if BACKEND == "ollama" else VLLM}
    try:
        models = _ollama_tags() if BACKEND == "ollama" else _vllm_models()
        st["models_installed"] = models
        base = MODEL.split(":")[0]
        st["model_present"] = any(m == MODEL or m.split(":")[0] == base
                                  for m in models)
        st["vision_capable"] = any(h in MODEL.lower() for h in VISION_HINTS)
        st["available"] = bool(st["model_present"] and st["vision_capable"])
        if not st["model_present"]:
            st["error"] = ("model %r not installed. Run: ollama pull %s"
                           % (MODEL, MODEL)) if BACKEND == "ollama" else \
                          "model %r not served by vLLM" % MODEL
        elif not st["vision_capable"]:
            st["error"] = ("%r looks like a TEXT-only model; it cannot read page "
                           "images. Pull a vision model, e.g. ollama pull qwen2.5vl:7b"
                           % MODEL)
    except Exception as ex:
        st["error"] = "%s not reachable at %s: %s" % (BACKEND, st["endpoint"], ex)
    return st
