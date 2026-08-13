#!/usr/bin/env python3
"""
Pre-flight check. Run this before any extraction run.

    python3 scripts/health.py

Reports every prerequisite separately so a failure names its own cause:
DocLayout weights, doclayout-yolo import, CUDA/GPU, VLM backend and whether the
selected model can actually see images, MongoDB, and input pages on disk.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def line(state, label, detail=""):
    print("[%s] %-26s %s" % (state, label, detail))


def probe_vlm():
    """Actually call the model once. Availability != working."""
    from PIL import Image, ImageDraw
    from epaper import vlm
    im = Image.new("RGB", (420, 240), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, 410, 70], outline=(0, 0, 0), width=3)
    d.text((24, 32), "MINISTER RESIGNS", fill=(0, 0, 0))
    d.text((24, 110), "New Delhi, Aug 12 -- the minister quit on Tuesday.", fill=(0, 0, 0))
    t0 = time.time()
    try:
        out = vlm.chat_json(
            'Read this image. Return ONLY JSON: '
            '{"headline":"...","body":"...","is_readable":true}',
            im, max_edge=800, max_tokens=400, timeout=300)
    except Exception as ex:
        line(BAD, "VLM live probe", str(ex)[:140])
        return False
    dt = time.time() - t0
    hl = (out or {}).get("headline") or ""
    ok = "MINISTER" in hl.upper() or "RESIGN" in hl.upper()
    line(OK if ok else WARN, "VLM live probe",
         "%.1fs  headline=%r%s" % (dt, hl[:50],
                                   "" if ok else "  <- did not read the image correctly"))
    if dt > 60:
        line(WARN, "VLM speed",
             "%.0fs for a tiny image; a full page will be far slower" % dt)
    return ok


def main():
    print("\nE-Paper pipeline health\n" + "=" * 72)

    from epaper import local_backend, vlm
    h = local_backend.health()

    line(OK if h["weights_present"] else BAD, "DocLayout weights", h["weights_path"])
    line(OK if h["doclayout_yolo"] else BAD, "doclayout-yolo import",
         h.get("doclayout_error", "importable"))
    line(OK if h["cuda"] else WARN, "CUDA",
         h.get("gpu") or h.get("torch_error", "no GPU visible — CPU will be very slow"))
    if h.get("torch"):
        line(OK, "torch", h["torch"])

    v = h["vlm"]
    line(OK if v.get("available") else BAD,
         "VLM %s" % v["backend"],
         v.get("error") or "%s @ %s" % (v["model"], v["endpoint"]))
    if v.get("num_ctx"):
        line(OK if v["num_ctx"] >= 8192 else WARN, "context window",
             "num_ctx=%d%s" % (v["num_ctx"],
                               "" if v["num_ctx"] >= 8192 else "  <- too small for a full page"))
    if v.get("models_installed"):
        line(OK, "models installed", ", ".join(v["models_installed"])[:120])

    # MongoDB + inputs
    try:
        from epaper.store import Store
        s = Store()
        if s.ping():
            c = s.counts()
            line(OK, "mongodb", "pages=%d articles=%d" % (c["pages"], c["articles"]))
            missing = 0
            total = 0
            for pg in s.db.pages.find({}, {"image_path": 1}):
                total += 1
                if not os.path.exists(pg.get("image_path", "")):
                    missing += 1
            line(OK if missing == 0 else WARN, "page images on disk",
                 "%d/%d present" % (total - missing, total))
        else:
            line(BAD, "mongodb", "not reachable at localhost:27017")
    except Exception as ex:
        line(BAD, "mongodb", str(ex)[:90])

    if h["ready"] and "--probe" in sys.argv:
        print("-" * 72)
        probe_vlm()

    print("=" * 72)
    if h["ready"]:
        if "--probe" not in sys.argv:
            print("Tip: re-run with --probe to make one real model call first.\n")
        print("READY — run:\n"
              "  python3 scripts/build_all_pdfs.py --paper statesman "
              "--max-pages 2 --backend local\n")
        return 0

    print("NOT READY. Fix the FAIL lines above.\n")
    if not h["doclayout_yolo"]:
        print("  source ~/epaper-venv/bin/activate")
    if not v.get("available"):
        if v.get("backend") == "ollama" and not v.get("model_present", True):
            print("  ollama pull %s" % v["model"])
        elif not v.get("vision_capable", True):
            print("  Selected model is text-only. Pull a vision model:")
            print("    ollama pull qwen2.5vl:7b")
            print("  then: export EPAPER_VLM=qwen2.5vl:7b")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
