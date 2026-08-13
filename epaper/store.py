"""
MongoDB persistence.

Three collections:
  papers    - one doc per title/edition, plus what the discovery probe found
  pages     - one doc per printed page, with the local image path
  articles  - one doc per extracted news item; the actual product

`text_source` on an article is the field to trust when judging quality:
  "publisher" - exact text from the publisher's own CMS. No OCR involved.
  "ocr"       - recognised from the page image.
  "vlm"       - produced by a vision model reading the page.
"""

from __future__ import annotations

import datetime as _dt

from pymongo import ASCENDING, MongoClient


class Store:
    def __init__(self, uri: str = "mongodb://localhost:27017", db: str = "epaper"):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=4000)
        self.db = self.client[db]
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.db.papers.create_index([("host", ASCENDING)], unique=True)
        self.db.pages.create_index(
            [("paper", ASCENDING), ("edition", ASCENDING),
             ("date", ASCENDING), ("page_no", ASCENDING)], unique=True)
        self.db.articles.create_index([("article_id", ASCENDING)], unique=True)
        self.db.articles.create_index(
            [("paper", ASCENDING), ("date", ASCENDING), ("page_no", ASCENDING)])

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    # -- writers ---------------------------------------------------------

    def save_paper(self, doc: dict):
        doc["updated_at"] = _dt.datetime.now()
        self.db.papers.update_one({"host": doc["host"]}, {"$set": doc}, upsert=True)

    def save_page(self, doc: dict):
        doc["updated_at"] = _dt.datetime.now()
        key = {k: doc[k] for k in ("paper", "edition", "date", "page_no")}
        self.db.pages.update_one(key, {"$set": doc}, upsert=True)

    def save_article(self, doc: dict):
        doc["updated_at"] = _dt.datetime.now()
        self.db.articles.update_one({"article_id": doc["article_id"]},
                                    {"$set": doc}, upsert=True)

    # -- readers ---------------------------------------------------------

    def counts(self) -> dict:
        return {c: self.db[c].estimated_document_count()
                for c in ("papers", "pages", "articles")}

    def pages_needing_segmentation(self, limit: int = 100) -> list:
        return list(self.db.pages.find({"segmented": {"$ne": True}}).limit(limit))


def article_id(paper: str, date: str, page_no, seq) -> str:
    """e.g. tribune-delhi-2026-08-12-p01-a03"""
    slug = "".join(c if c.isalnum() else "-" for c in paper.lower()).strip("-")
    return "%s-%s-p%s-a%s" % (slug, date, str(page_no).zfill(2), str(seq).zfill(2))
