#!/usr/bin/env python3
"""
DocLayout-YOLO as an HTTP service. Runs ON the GPU box; the pipeline stays on
the laptop and calls in.

    ~/epaper-venv/bin/python server/layout_service.py --port 8001

Endpoints
    GET  /health   -> {ok, cuda, gpu, weights, model_loaded}
    POST /layout   -> {"image_b64": "<base64 jpeg>", "conf": 0.2}
                      {"elements":[{n,kind,bbox_px,conf}], "size":[W,H], "ms":int}

Stdlib only, deliberately. The GPU box has a broken NVML (nvidia-smi fails with
a driver/library mismatch) which is what blocks vLLM, so this avoids anything
that queries NVML at import. torch's CUDA runtime is unaffected and works.

The model is loaded once at startup and reused; a T4 holds it comfortably
alongside Ollama's 6 GB of qwen2.5vl.
"""
import argparse
import base64
import io
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

Image.MAX_IMAGE_PIXELS = None

WEIGHTS = os.path.expanduser(os.environ.get(
    "EPAPER_YOLO", "~/models/doclayout_yolo_docstructbench_imgsz1024.pt"))
DEVICE = os.environ.get("EPAPER_DEVICE", "cuda:0")

MODEL = None
NAMES = {}
FALLBACK_NAMES = ["title", "plain_text", "abandon", "figure", "figure_caption",
                  "table", "table_caption", "table_footnote", "isolate_formula",
                  "formula_caption"]


def load():
    global MODEL, NAMES
    if MODEL is None:
        from doclayout_yolo import YOLOv10
        MODEL = YOLOv10(WEIGHTS)
        NAMES = getattr(MODEL, "names", None) or {}
        print("loaded %s on %s | classes=%s" % (WEIGHTS, DEVICE, NAMES), flush=True)
    return MODEL


def class_name(i, res_names):
    for src in (res_names, NAMES):
        if isinstance(src, dict) and i in src:
            return src[i]
        if isinstance(src, (list, tuple)) and i < len(src):
            return src[i]
    return FALLBACK_NAMES[i] if i < len(FALLBACK_NAMES) else "other"


def run_layout(img: Image.Image, conf: float):
    t0 = time.time()
    model = load()
    res = model.predict(img, imgsz=1024, conf=conf, device=DEVICE, verbose=False)[0]
    res_names = getattr(res, "names", None) or {}

    out = []
    for box, cls, cf in zip(res.boxes.xyxy.tolist(),
                            res.boxes.cls.tolist(),
                            res.boxes.conf.tolist()):
        name = class_name(int(cls), res_names)
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
    return {"elements": out, "size": list(img.size),
            "ms": int((time.time() - t0) * 1000)}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        print("%s - %s" % (self.address_string(), fmt % a), flush=True)

    def do_GET(self):
        if self.path.rstrip("/") not in ("/health", ""):
            return self._send(404, {"error": "not found"})
        st = {"ok": True, "weights": WEIGHTS,
              "weights_present": os.path.exists(WEIGHTS),
              "model_loaded": MODEL is not None, "device": DEVICE}
        try:
            import torch
            st["torch"] = torch.__version__
            st["cuda"] = bool(torch.cuda.is_available())
            st["gpu"] = torch.cuda.get_device_name(0) if st["cuda"] else None
        except Exception as ex:
            st["cuda"] = False
            st["torch_error"] = str(ex)[:200]
        st["ok"] = bool(st["weights_present"])
        self._send(200, st)

    def do_POST(self):
        if self.path.rstrip("/") != "/layout":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode())
            raw = base64.b64decode(req["image_b64"])
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            self._send(200, run_layout(img, float(req.get("conf", 0.20))))
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._send(500, {"error": "%s: %s" % (type(ex).__name__, ex)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--preload", action="store_true",
                    help="load weights at startup instead of first request")
    a = ap.parse_args()
    if a.preload:
        load()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print("layout service on http://%s:%d  (weights=%s device=%s)"
          % (a.host, a.port, WEIGHTS, DEVICE), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
