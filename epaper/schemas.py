"""
Pydantic schemas for pipeline article validation.

Missing fields stay None/empty — never invent defaults for content.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class BBoxPx(BaseModel):
    """Pixel box [x1, y1, x2, y2] on the rendered page image."""
    x1: float
    y1: float
    x2: float
    y2: float

    @classmethod
    def from_list(cls, v: list | tuple) -> "BBoxPx":
        if not v or len(v) != 4:
            raise ValueError("bbox must have 4 numbers")
        x1, y1, x2, y2 = (float(x) for x in v)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bbox must have positive area")
        return cls(x1=x1, y1=y1, x2=x2, y2=y2)

    def as_list(self) -> list[float]:
        return [self.x1, self.y1, self.x2, self.y2]


class RegionBox(BaseModel):
    type: str = ""
    bbox: list[float] = Field(default_factory=list)
    text: str = ""

    @field_validator("bbox")
    @classmethod
    def _bbox4(cls, v: list) -> list:
        if v and len(v) != 4:
            raise ValueError("region bbox must be length 4")
        return v


class VLMExtraction(BaseModel):
    headline: str = ""
    subheadline: Optional[str] = None
    kicker: Optional[str] = None
    byline: Optional[str] = None
    dateline: Optional[str] = None
    location: Optional[str] = None
    body: str = ""
    summary: Optional[str] = None
    photo_captions: list[str] = Field(default_factory=list)
    continued_on_page: Optional[Any] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    transcription_confidence: Optional[float] = None
    raw: Optional[dict] = None


class ExtractedArticle(BaseModel):
    """Validated article after grouping + optional VLM read."""
    seq: int = 0
    type: Literal["article", "ad", "masthead"] = "article"
    headline: str = ""
    subheadline: Optional[str] = None
    body: str = ""
    summary: Optional[str] = None
    byline: Optional[str] = None
    dateline: Optional[str] = None
    location: Optional[str] = None
    category: str = ""
    subcategory: str = ""
    keywords: list[str] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)
    places: list[str] = Field(default_factory=list)
    bbox_px: list[float] = Field(default_factory=list)
    bbox: list[float] = Field(default_factory=list)
    crop: str = ""
    crop_path: str = ""
    region_boxes: list[RegionBox] = Field(default_factory=list)
    has_photo: bool = False
    n_elements: int = 0
    grouping_confidence: Optional[float] = None
    ocr_backend: Optional[str] = None
    ocr_error: Optional[str] = None
    headline_ocr: Optional[str] = None
    text_source: str = "vlm"
    extraction_status: str = "ok"  # ok | partial | failed
    page_no: int = 0
    paper: str = ""
    edition: str = ""
    publication_date: str = ""

    @field_validator("bbox_px", "bbox")
    @classmethod
    def _box_len(cls, v: list) -> list:
        if v and len(v) != 4:
            raise ValueError("bbox must be length 4")
        return v


class StageResult(BaseModel):
    name: str
    status: str  # ok | failed | skipped
    elapsed_s: float = 0.0
    error: str = ""
    meta: dict = Field(default_factory=dict)


class PagePipelineResult(BaseModel):
    page_path: str = ""
    page_no: int = 1
    width: int = 0
    height: int = 0
    status: str = "FAILED"  # see pipeline.STAGES / terminal statuses
    stages: list[StageResult] = Field(default_factory=list)
    elements: list[dict] = Field(default_factory=list)
    articles: list[ExtractedArticle] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    timings: dict[str, float] = Field(default_factory=dict)


def validate_vlm_dict(data: dict) -> VLMExtraction:
    if not isinstance(data, dict):
        return VLMExtraction(raw={"_invalid": True})
    cleaned = dict(data)
    cleaned["raw"] = data
    # Coerce scalar list-ish fields
    for k in ("keywords", "people", "organizations", "events", "places",
              "topics", "photo_captions"):
        v = cleaned.get(k)
        if v is None:
            cleaned[k] = []
        elif isinstance(v, str):
            cleaned[k] = [v] if v.strip() else []
        elif not isinstance(v, list):
            cleaned[k] = []
    return VLMExtraction.model_validate(cleaned)


def article_from_region(r: dict, *, seq: int = 0, crop: str = "",
                        page_no: int = 0) -> ExtractedArticle:
    return ExtractedArticle(
        seq=seq,
        type=r.get("type") if r.get("type") in ("article", "ad", "masthead")
        else "article",
        headline=(r.get("headline_visible") or r.get("headline") or "").strip(),
        body=(r.get("body") or "").strip(),
        byline=r.get("byline") or None,
        dateline=r.get("dateline") or None,
        bbox_px=list(r.get("bbox_px") or r.get("bbox") or []),
        bbox=list(r.get("bbox") or []),
        crop=crop or (r.get("crop") or ""),
        region_boxes=[RegionBox.model_validate(x)
                      for x in (r.get("region_boxes") or [])
                      if isinstance(x, dict)],
        has_photo=bool(r.get("has_photo")),
        n_elements=len(r.get("elements") or []),
        grouping_confidence=r.get("grouping_confidence"),
        ocr_error=r.get("ocr_error"),
        ocr_backend=r.get("ocr_backend"),
        headline_ocr=r.get("headline_ocr"),
        extraction_status=r.get("extraction_status") or "ok",
        page_no=page_no or int(r.get("page_no") or 0),
    )
