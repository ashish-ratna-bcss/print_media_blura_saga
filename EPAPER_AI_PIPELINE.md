# E-Paper AI Pipeline (current only)

## Stack

| Component | Technology |
|-----------|------------|
| PDF | PyMuPDF |
| Layout | DocLayout-YOLO |
| OCR | IndicOCR (`POST /extract`) |
| Grouping | Python + Shapely |
| Crops | Pillow (`epaper/crop.py`) |
| Article understanding | Qwen2.5-VL |
| Validation | Pydantic |

## Flow

```
Acquire / upload
 → PyMuPDF render (pdf_render.py)
 → optional quality (preprocess.py)
 → DocLayout-YOLO (local_backend.detect_elements)
 → Shapely grouping + IndicOCR headlines (group_articles)
 → Pillow crops (crop.crop_region)
 → Qwen2.5-VL (local_backend.transcribe)
 → Pydantic (schemas.py)
 → MongoDB (corpus / jobs)
```

Orchestrator: `epaper/pipeline.process_page`.

## Removed (do not use)

- Poppler / `pdftoppm`
- Tesseract / pytesseract
- Claude `segment.py` layout path
- Geometric `autosegment.py`
- PaddleOCR

## Env

See `.env.example`.
