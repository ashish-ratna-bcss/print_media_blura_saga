"""
PDF → page image via PyMuPDF.

Active upload path writes the same artefact the rest of the pipeline expects:
`page.jpg` at 200 DPI JPEG quality 92. Pixel coordinates for embedded text
use the same scale (dpi/72) so they line up with DocLayout-YOLO boxes on that
JPEG.

Pages are rendered one at a time — the Document stays open, but only the
requested page is rasterised into memory.

Docs: https://pymupdf.readthedocs.io/en/latest/
"""

from __future__ import annotations

import json
import os
from typing import Iterator

from PIL import Image

# Official package import, aliased as `fitz` for the traditional API surface.
# https://pymupdf.readthedocs.io/en/latest/
import pymupdf as fitz

DEFAULT_DPI = 200
JPEG_QUALITY = 92


class PDFRenderError(RuntimeError):
    """User-facing PDF open/render failure."""


def scale_for(dpi: int = DEFAULT_DPI) -> float:
    """PDF user-space points → rendered pixels (72 pt per inch)."""
    return float(dpi) / 72.0


def open_document(path: str, password: str | None = None) -> fitz.Document:
    """
    Open a PDF. Raises PDFRenderError for missing/corrupt/encrypted/empty files.
    Caller must close the returned document.
    """
    if not path or not os.path.isfile(path):
        raise PDFRenderError("PDF not found: %s" % path)
    try:
        doc = fitz.open(path)
    except Exception as ex:
        raise PDFRenderError("cannot open PDF (%s): %s"
                             % (os.path.basename(path), ex)) from ex

    try:
        if doc.is_encrypted or getattr(doc, "needs_pass", False):
            ok = False
            if password:
                try:
                    ok = bool(doc.authenticate(password))
                except Exception:
                    ok = False
            if not ok:
                doc.close()
                raise PDFRenderError(
                    "PDF is encrypted or password-protected; provide a password "
                    "or unlock the file before upload")
        if doc.page_count < 1:
            doc.close()
            raise PDFRenderError("PDF has no pages")
        # Reject clearly non-PDF opens that MuPDF sometimes accepts as empty shells.
        if doc.is_pdf is False and not str(path).lower().endswith(".pdf"):
            doc.close()
            raise PDFRenderError("unsupported document type (expected PDF)")
    except PDFRenderError:
        raise
    except Exception as ex:
        try:
            doc.close()
        except Exception:
            pass
        raise PDFRenderError("PDF open failed: %s" % ex) from ex
    return doc


def page_dimensions(doc: fitz.Document, page_index: int = 0,
                    dpi: int = DEFAULT_DPI) -> dict:
    """Page size in PDF points and in rendered pixels at `dpi`."""
    if page_index < 0 or page_index >= doc.page_count:
        raise PDFRenderError("page %d out of range (1-%d)"
                             % (page_index + 1, doc.page_count))
    page = doc.load_page(page_index)
    rect = page.rect
    s = scale_for(dpi)
    return {
        "page_index": page_index,
        "page_no": page_index + 1,
        "width_pt": float(rect.width),
        "height_pt": float(rect.height),
        "width_px": int(round(rect.width * s)),
        "height_px": int(round(rect.height * s)),
        "dpi": dpi,
        "scale": s,
        "page_count": doc.page_count,
    }


def render_page(doc: fitz.Document, dest: str, page_index: int = 0,
                dpi: int = DEFAULT_DPI, quality: int = JPEG_QUALITY) -> dict:
    """
    Rasterise one page to JPEG at `dpi`. Only that page is rendered.
    Returns dimension metadata (same shape as page_dimensions).
    """
    meta = page_dimensions(doc, page_index, dpi)
    page = doc.load_page(page_index)
    mat = fitz.Matrix(meta["scale"], meta["scale"])
    try:
        pix = page.get_pixmap(matrix=mat, alpha=False)
    except Exception as ex:
        raise PDFRenderError("failed to render page %d: %s"
                             % (page_index + 1, ex)) from ex
    Image.frombytes("RGB", (pix.width, pix.height), pix.samples).save(
        dest, "JPEG", quality=quality)
    # Prefer actual pixmap size (rounding) over predicted.
    meta["width_px"] = pix.width
    meta["height_px"] = pix.height
    meta["path"] = dest
    return meta


def extract_text_blocks(doc: fitz.Document, page_index: int = 0,
                        dpi: int = DEFAULT_DPI) -> list:
    """
    Embedded text spans with bboxes in *rendered pixel* coordinates.

    Empty for scanned pages with no text layer. Coordinates use the same
    dpi/72 scale as render_page so they align with YOLO boxes on page.jpg.
    """
    if page_index < 0 or page_index >= doc.page_count:
        raise PDFRenderError("page %d out of range (1-%d)"
                             % (page_index + 1, doc.page_count))
    page = doc.load_page(page_index)
    s = scale_for(dpi)
    out = []
    try:
        data = page.get_text("dict")
    except Exception:
        return out
    for block in data.get("blocks") or []:
        if block.get("type", 0) != 0:          # 0 = text, 1 = image
            continue
        for line in block.get("lines") or []:
            for span in line.get("spans") or []:
                text = (span.get("text") or "").strip()
                if not text:
                    continue
                x0, y0, x1, y1 = span.get("bbox") or (0, 0, 0, 0)
                out.append({
                    "text": text,
                    "bbox_px": [int(round(x0 * s)), int(round(y0 * s)),
                                int(round(x1 * s)), int(round(y1 * s))],
                    "bbox_pt": [float(x0), float(y0), float(x1), float(y1)],
                    "size": float(span.get("size") or 0),
                    "font": span.get("font") or "",
                })
    return out


def extract_embedded_images(doc: fitz.Document, page_index: int,
                            out_dir: str) -> list:
    """
    Write embedded images from one page into out_dir.
    Returns metadata list; skips tiny icons (<32px).
    """
    if page_index < 0 or page_index >= doc.page_count:
        raise PDFRenderError("page %d out of range" % (page_index + 1))
    os.makedirs(out_dir, exist_ok=True)
    page = doc.load_page(page_index)
    saved = []
    for i, img in enumerate(page.get_images(full=True) or []):
        xref = img[0]
        try:
            pix = fitz.Pixmap(doc, xref)
            if pix.n > 4:                       # CMYK etc.
                pix = fitz.Pixmap(fitz.csRGB, pix)
            if pix.width < 32 or pix.height < 32:
                continue
            name = "embed_p%02d_%02d.png" % (page_index + 1, i + 1)
            path = os.path.join(out_dir, name)
            pix.save(path)
            saved.append({"path": name, "width": pix.width, "height": pix.height,
                          "xref": xref})
        except Exception:
            continue
    return saved


def iter_pages(path: str, password: str | None = None
               ) -> Iterator[tuple[fitz.Document, int]]:
    """
    Yield (doc, page_index) one page at a time without rasterising.
    Caller must not close `doc` until the iterator is exhausted; use
    render_pdf_pages for the common write loop instead.
    """
    doc = open_document(path, password)
    try:
        for i in range(doc.page_count):
            yield doc, i
    finally:
        doc.close()


def render_pdf_pages(path: str, out_dir: str, *, dpi: int = DEFAULT_DPI,
                     password: str | None = None,
                     extract_text: bool = True,
                     extract_images: bool = False,
                     page_indices: list | None = None) -> list:
    """
    Render selected pages (default: all) as page_01.jpg, page_02.jpg, …
    One page pixmap at a time. Optional text/image sidecars per page.
    """
    os.makedirs(out_dir, exist_ok=True)
    doc = open_document(path, password)
    results = []
    try:
        indices = page_indices if page_indices is not None else list(
            range(doc.page_count))
        for i in indices:
            dest = os.path.join(out_dir, "page_%02d.jpg" % (i + 1))
            meta = render_page(doc, dest, i, dpi=dpi)
            if extract_text:
                spans = extract_text_blocks(doc, i, dpi=dpi)
                side = os.path.join(out_dir, "page_%02d_text.json" % (i + 1))
                with open(side, "w", encoding="utf-8") as fh:
                    json.dump({"page_no": i + 1, "dpi": dpi,
                               "scale": scale_for(dpi), "spans": spans},
                              fh, ensure_ascii=False)
                meta["text_spans"] = len(spans)
                meta["text_sidecar"] = side
            if extract_images:
                img_dir = os.path.join(out_dir, "embeds_p%02d" % (i + 1))
                meta["embedded_images"] = extract_embedded_images(
                    doc, i, img_dir)
            results.append(meta)
    finally:
        doc.close()
    return results


def pdf_first_page_to_jpeg(path: str, dest: str, *, dpi: int = DEFAULT_DPI,
                           password: str | None = None,
                           text_sidecar: str | None = None,
                           embeds_dir: str | None = None) -> dict:
    """
    Active helper used by web uploads: first page → dest JPEG + optional sidecars.
    """
    doc = open_document(path, password)
    try:
        meta = render_page(doc, dest, 0, dpi=dpi)
        meta["engine"] = "pymupdf"
        if text_sidecar is not None:
            spans = extract_text_blocks(doc, 0, dpi=dpi)
            with open(text_sidecar, "w", encoding="utf-8") as fh:
                json.dump({"page_no": 1, "dpi": dpi, "scale": scale_for(dpi),
                           "spans": spans}, fh, ensure_ascii=False)
            meta["text_spans"] = len(spans)
            meta["text_sidecar"] = text_sidecar
        if embeds_dir is not None:
            meta["embedded_images"] = extract_embedded_images(doc, 0, embeds_dir)
        return meta
    finally:
        doc.close()
