# AI Pipeline Audit (current stack only)

Supersedes older Poppler / Tesseract / Claude / geometric-detector notes.

## Active modules

| Stage | Module |
|-------|--------|
| PDF | `epaper/pdf_render.py` (PyMuPDF) |
| Quality | `epaper/preprocess.py` |
| Layout | `epaper/local_backend.detect_elements` (DocLayout-YOLO) |
| Grouping | `epaper/geometry.py` + `_assign_to_titles` (Shapely) |
| Headline OCR | `epaper/ocr.py` → `indic_ocr.py` |
| Crops | `epaper/crop.py` (Pillow) |
| VLM | `epaper/local_backend.transcribe` → `vlm.py` |
| Validation | `epaper/schemas.py` |
| Orchestration | `epaper/pipeline.py` |

## Deleted legacy

- `epaper/autosegment.py`
- `epaper/segment.py` (Claude)
- Poppler helpers in `pdf_render.py`
- Tesseract path in `ocr.py`
