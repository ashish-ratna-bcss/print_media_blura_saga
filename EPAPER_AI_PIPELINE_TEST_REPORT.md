# E-Paper AI Pipeline Test Report

**Date:** 2026-11-16  
**Host:** developer Mac (torch CPU for YOLO; remote IndicOCR + Qwen VL)

## Automated unit tests

```
pytest tests/test_pipeline_schemas.py -q
.......  (7 passed)
```

Coverage: bbox validation, VLM dict coerce, article_from_region OCR metadata, IndicOCR normalize/scale/offset, xywh box parse, quality_report.

## Live service checks

| Check | Result | Time |
|-------|--------|------|
| IndicOCR `GET /health` | **OK** `model_loaded`, engine `qwen` | ~0.2 s |
| IndicOCR headline crop `POST /extract` | **OK** Telugu text returned | **2.64 s** |
| Qwen2.5-VL `GET /v1/models` | **OK** | — |
| VLM `transcribe` on `p04_a01.jpg` | **OK** headline + body (192 chars) | **9.3 s** |

## Layout + grouping (real page)

**Page:** `web/data/jobs/a955822541c5/p04.jpg` (Telugu multi-article)

| Metric | Value |
|--------|-------|
| YOLO elements | **99** (55 plain text, 29 title, 10 figure, …) |
| YOLO time (CPU) | **4.34 s** |
| `process_page(..., with_vlm=False)` | **COMPLETED** |
| Articles / crops | **20** |
| Wall time (layout+OCR headlines+crops) | **82.9 s** |
| Sample OCR headlines | Non-empty Telugu strings on most titles |

**Note:** A different page (`5179dc29bc73/p01.jpg`) returned only 1 YOLO `figure` at conf 0.25 — treat as page/content specific, not a pipeline crash. Pipeline still completed with available regions.

## Accuracy

No ground-truth CER/WER labels were available in-repo. **No invented accuracy %.**  
Qualitative: IndicOCR returns Telugu script for Telugu crops; VLM returns structured JSON with headline/body; placeholder echo filter retained.

## Known limitations

1. Mac YOLO is CPU-only here — production GPU will be faster.
2. Headline OCR over ~29 titles dominates page time (~80 s) when using remote IndicOCR.
3. Full multi-page PDF → Mongo end-to-end acceptance not run in this session (requires long VLM budget per article); stages were validated independently + one VLM crop live.
4. Byline-as-title rejection remains disabled (prior measured coverage regression).

## How to re-run

```bash
source .venv/bin/activate
pytest tests/test_pipeline_schemas.py -q
python scripts/test_indic_ocr.py web/data/jobs/<id>/p04_a01.jpg
python -c "from epaper.pipeline import process_page; \
 print(process_page('web/data/jobs/.../p04.jpg', with_vlm=False).status)"
```
