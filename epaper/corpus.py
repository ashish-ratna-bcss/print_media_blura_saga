"""
The article corpus: extract once, search forever.

The pipeline as it stands is extract-on-query. Asking for Delhi Police stories
re-fetches the page, re-runs layout, re-groups, re-reads and re-judges -- 177 to
475 seconds measured -- and does all of it again for the next question about the
same page. Reading a page is expensive and its result never changes; answering a
question about text already read should be instant. Measured here: 262 ms.

Four collections, each answering a different question:

    epapers         one issue -- this title, this date, how many pages
    pages           one printed page, its image, its dimensions
    articles        one story, with the regions it was assembled from
    search_history  what was asked, what came back, how long it took

`articles` carries `epaper_id` and `page_id` rather than repeating the paper
name and date on every row, but ALSO keeps `paper`/`date`/`page_number`
denormalised. That is deliberate: search filters by paper and date on almost
every query, and a $lookup per query to recover them would cost more than the
duplication. Mongo is not a relational store and pretending otherwise here
would be slower, not purer.

Two retrieval paths, because the two kinds of query are not alike:

  A resolved beat ("Delhi Police") expands to a known vocabulary, so retrieval
  is an indexed `keywords: {$in: [...]}` over terms computed at ingest. Exact,
  multikey-indexed, and nothing is tokenised at query time.

  A free-text query has no vocabulary, so it falls back to the `$text` index
  over `full_text`.

Both rescore in Python through keywords.score, which is where weighting, the
headline bonus and the region gate live. Mongo decides WHICH documents;
keywords.py decides HOW WELL. Neither alone is enough -- textScore ranks a
passing mention of "police" above an arrest story that never says the word.

`full_text` exists so the text index has one field to weigh instead of two.
It is headline + body concatenated at write time, which also means a phrase
spanning the headline-to-body boundary is findable.

Language: the text index is created with default_language "none" on purpose.
Mongo has no Hindi stemmer, and applying the English one to a bilingual corpus
mangles Devanagari without helping. Morphology is handled by the keyword
layer's prefix matching, which runs on every candidate anyway.

Driver note: this is synchronous PyMongo, called from FastAPI's threadpool. See
README -- async was considered and is not the bottleneck.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re

from pymongo import ASCENDING, DESCENDING, MongoClient, TEXT

from . import config, keywords

# Blend of the two rankers. Mongo knows which documents contain the words; the
# beat vocabulary knows which of them are on the beat, and carries the region
# gate. Weighted toward the beat because that is the half that understands what
# the terms mean.
W_LEX, W_BEAT = 0.40, 0.60

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_PUNCT = re.compile(r"[^\w\sऀ-ॿ]+")

# Which (host, database) pairs this process has already reported. Silence is
# the wrong default here: every write bug this module has had was a write that
# landed somewhere unexpected, and one line at startup is what makes "the
# server is still holding the old config" visible before a job runs, not after.
_seen: set = set()


def _announce(host: str, name: str):
    if (host, name) in _seen:
        return
    _seen.add((host, name))
    print("corpus: %s / %s" % (host, name))


class Corpus:
    """
    Connection defaults come from epaper.config, which reads `.env`.

    Both arguments stay overridable so a test can point at a scratch database
    without touching the environment -- which is how the regression harness
    stopped writing its fixtures into the live corpus.
    """

    def __init__(self, uri: str | None = None, db: str | None = None,
                 *, quiet: bool = False):
        self.uri = config.mongo_uri(uri)
        self.name = config.mongo_db(db)
        self.client = MongoClient(self.uri, **config.mongo_kwargs())
        # Deliberately NOT get_default_database(). See epaper/config.py: the
        # database named in an Atlas connection string belongs to whoever
        # generated it, and on a shared cluster that is not us.
        self.db = self.client[self.name]
        if not quiet:
            _announce(config.hosts(self.uri), self.name)
        self.epapers = self.db.epapers
        self.pages = self.db.pages
        self.articles = self.db.articles
        self.history = self.db.search_history
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.epapers.create_index([("name", ASCENDING), ("edition", ASCENDING),
                                   ("date", ASCENDING)], unique=True)
        self.epapers.create_index([("date", DESCENDING)])

        self.pages.create_index([("epaper_id", ASCENDING),
                                 ("page_number", ASCENDING)], unique=True)

        self.articles.create_index([("uid", ASCENDING)], unique=True, sparse=True)
        self.articles.create_index([("epaper_id", ASCENDING)])
        self.articles.create_index([("page_id", ASCENDING)])
        self.articles.create_index([("keywords", ASCENDING)])
        self.articles.create_index([("category", ASCENDING)])
        self.articles.create_index([("date", DESCENDING)])
        self.articles.create_index([("paper", ASCENDING), ("date", DESCENDING)])
        self.articles.create_index([("beats", ASCENDING), ("date", DESCENDING)])
        # There used to be one index per beat, so `sort(beat_scores.<id>)` was
        # an index scan. That was written when there was one beat. With 36
        # states it would be 36 indexes against MongoDB's hard limit of 64 per
        # collection, and it would break outright the first time someone added
        # beats from the backend -- which is now a supported thing to do.
        #
        # The multikey index on `beats` does the selecting instead, and it is
        # selective: an article is tagged only with the forces whose patch it
        # is actually on. Mongo then does a blocking sort over what survives.
        # That is bounded by a 32 MB sort buffer, which a filtered slice of one
        # beat does not approach. EPAPER_BEAT_INDEX exists for the day one beat
        # grows big enough to want its own index back.
        for beat_id in [b for b in
                        (os.environ.get("EPAPER_BEAT_INDEX") or "").split(",")
                        if b.strip()]:
            self.articles.create_index(
                [("beat_scores.%s" % beat_id.strip(), DESCENDING)])
        # Mongo allows one text index per collection, and create_index is a
        # silent no-op when a text index already exists over DIFFERENT fields.
        # That is how an index left over from an earlier schema kept pointing at
        # headline+body while every query targeted full_text -- free-text search
        # quietly returned nothing. Detect the mismatch and say so.
        self.text_index_ok = True
        try:
            existing = {n: s for n, s in self.articles.index_information().items()
                        if any(k[0] in ("_fts", "_ftsx") for k in s.get("key", []))}
            if not existing:
                self.articles.create_index([("full_text", TEXT)],
                                           default_language="none",
                                           name="corpus_text")
            elif list((list(existing.values())[0].get("weights") or {})) != ["full_text"]:
                self.text_index_ok = False
                print("corpus: text index %r covers %s, not full_text -- "
                      "free-text search will not work. Drop it to rebuild."
                      % (list(existing)[0],
                         list((list(existing.values())[0].get("weights") or {}))))
        except Exception as ex:
            self.text_index_ok = False
            print("corpus: could not verify text index: %s" % str(ex)[:120])

        self.history.create_index([("at", DESCENDING)])
        self.history.create_index([("query", ASCENDING)])

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    # -- write -----------------------------------------------------------

    def upsert_epaper(self, name, edition, date, *, source="", total_pages=0,
                      pdf_path=None, status="ready"):
        key = {"name": name or "unknown", "edition": edition or "", "date": date or ""}
        doc = dict(key, source=source, status=status, updated_at=_dt.datetime.now())
        if total_pages:
            doc["total_pages"] = total_pages
        if pdf_path:
            doc["pdf_path"] = pdf_path
        self.epapers.update_one(key, {"$set": doc,
                                      "$setOnInsert": {"created_at": _dt.datetime.now()}},
                                upsert=True)
        return self.epapers.find_one(key)["_id"]

    def upsert_page(self, epaper_id, page_number, *, image_path=None,
                    width=0, height=0, status="processed",
                    paper="", edition="", date=""):
        """
        Write a page under BOTH naming schemes.

        store.py and this module share the `pages` collection and disagree
        about field names: store.py holds a unique index on
        (paper, edition, date, page_no) while the corpus keys on
        (epaper_id, page_number). Leaving store.py's fields unset makes every
        page after the first collide on four nulls -- which is exactly what
        happened, and it is the same failure the `article_id` collision caused.
        Populating both satisfies both indexes without dropping an index a
        sibling module still relies on.
        """
        key = {"epaper_id": epaper_id, "page_number": int(page_number or 0)}
        doc = dict(key, status=status, updated_at=_dt.datetime.now(),
                   paper=paper or "", edition=edition or "", date=date or "",
                   page_no=int(page_number or 0))
        if image_path:
            doc["image_path"] = image_path
        if width:
            doc["width"] = int(width)
        if height:
            doc["height"] = int(height)
        self.pages.update_one(key, {"$set": doc,
                                    "$setOnInsert": {"created_at": _dt.datetime.now()}},
                              upsert=True)
        return self.pages.find_one(key)["_id"]

    def ingest(self, articles: list, *, job_id="", paper="", edition="",
               date="", default_page=0, page_images=None) -> dict:
        """
        Write a job's output as epaper -> pages -> articles. Idempotent per uid.

        Ads and mastheads are skipped: they are page furniture, and indexing
        them only adds noise to every future query.
        """
        epaper_id = self.upsert_epaper(paper, edition, date, source=job_id)
        page_ids, counts = {}, {"added": 0, "updated": 0, "skipped": 0}

        for a in articles:
            if (a.get("type") or "article") != "article":
                counts["skipped"] += 1
                continue
            headline = (a.get("headline") or "").strip()
            body = (a.get("body") or "").strip()
            if not headline and len(body) < 40:
                counts["skipped"] += 1            # nothing worth indexing
                continue

            page_number = int(a.get("page_ref") or a.get("page_no")
                              or default_page or 0)
            if page_number not in page_ids:
                img = (page_images or {}).get(page_number) or {}
                page_ids[page_number] = self.upsert_page(
                    epaper_id, page_number, image_path=img.get("path"),
                    width=img.get("width", 0), height=img.get("height", 0),
                    paper=paper, edition=edition, date=date)

            bbox = _as_list(a.get("bbox"))
            regions = _regions_of(a, bbox, headline)
            full_text = ("%s\n%s" % (headline, body)).strip()
            hits, beats, beat_scores, category = beats_for({"headline": headline,
                                                            "body": body})

            uid = uid_for(paper, date, page_number, headline, bbox)
            doc = {
                "uid": uid,
                # store.py holds a UNIQUE index on article_id, and this module
                # shares its collection. Leaving the field unset makes every
                # document collide on null -- observed: 13 articles ingested,
                # 1 survived. Derived from uid rather than store.py's `seq`,
                # which changes between extractions of the same page.
                "article_id": article_id_for(paper, date, page_number, uid),
                "epaper_id": epaper_id,
                "page_id": page_ids[page_number],
                "page_number": page_number,
                # Denormalised for filtering; see module docstring.
                "paper": paper, "edition": edition, "date": date,
                # store.py indexes (paper, date, page_no); mirror page_number
                # so both modules can read the collection consistently.
                "page_no": page_number,
                "job_id": job_id,
                "headline": headline,
                "body": body,
                "full_text": full_text,
                "category": category,
                "keywords": hits,
                "beats": beats,
                # Precomputed per-beat scores, so a beat query is an indexed
                # sort in Mongo instead of 292 regexes per candidate in Python.
                "beat_scores": beat_scores,
                "regions": regions,
                "byline": a.get("byline") or "",
                "dateline": a.get("dateline") or "",
                "lang": detect_lang(full_text),
                "bbox": bbox,
                "crop": a.get("crop"),
                "text_source": a.get("text_source") or "vlm",
                "updated_at": _dt.datetime.now(),
            }
            # Per-document, not per-batch. A single rejected write used to
            # abort the whole page: one article_id collision cost 12 of 13
            # articles. One bad document should cost one document.
            try:
                r = self.articles.update_one(
                    {"uid": uid},
                    {"$set": doc,
                     "$setOnInsert": {"created_at": _dt.datetime.now()}},
                    upsert=True)
                if r.upserted_id is not None:
                    counts["added"] += 1
                elif r.modified_count:
                    counts["updated"] += 1
                else:
                    counts["skipped"] += 1
            except Exception as ex:
                counts["failed"] = counts.get("failed", 0) + 1
                counts.setdefault("errors", []).append(
                    "%s: %s" % (headline[:40], str(ex)[:120]))

        self.epapers.update_one({"_id": epaper_id},
                                {"$set": {"article_count":
                                          self.articles.count_documents(
                                              {"epaper_id": epaper_id})}})
        return counts

    # -- read ------------------------------------------------------------

    def search(self, query: str, *, paper=None, date_from=None, date_to=None,
               beat=None, category=None, limit: int = 50,
               min_score: float = 0.0, record: bool = True) -> dict:
        """Hybrid retrieval over everything ever extracted. No model involved."""
        t0 = _dt.datetime.now()
        spec = keywords.expand(query)
        flt = {}
        if paper:
            flt["paper"] = paper
        if beat:
            flt["beats"] = beat
        if category:
            flt["category"] = category
        if date_from or date_to:
            flt["date"] = {}
            if date_from:
                flt["date"]["$gte"] = date_from
            if date_to:
                flt["date"]["$lte"] = date_to

        cap = max(limit * 4, 200)
        if spec["expanded"]:
            # Fast path. The score is already stored per article, so Mongo
            # ranks and truncates and only `limit` rows come back -- no
            # over-fetch, and no rescoring in Python at all.
            #
            # Selection is on `beats`, not on the term list. With one beat the
            # two were equivalent; with 36 sharing one policing vocabulary they
            # are not -- `keywords: {$in: ...}` matches every police story in
            # the country, then sorts 90% of them to the bottom. Filtering on
            # the beat that actually claims the article does the work in the
            # index instead. Off-patch stories drop out rather than trailing at
            # 0.20, which is also what someone who picked a force wants.
            field = "beat_scores.%s" % spec["beat"]
            flt["beats"] = spec["beat"]
            rows = list(self.articles.find(flt)
                        .sort(field, DESCENDING).limit(limit))
            out = []
            for r in rows:
                sc = (r.get("beat_scores") or {})
                blended = sc.get(spec["beat"])
                if blended is None:
                    # Written before scores were precomputed: fall back rather
                    # than drop the row, and let a backfill fix it properly.
                    s = keywords.score({"headline": r.get("headline", ""),
                                        "body": r.get("body", "")}, spec)
                    lexs = min(1.0, len(s["hits"]) / 8.0)
                    blended = round(W_LEX * lexs + W_BEAT * s["score"], 3)
                    hit = _hit(r, blended, lexs, s)
                else:
                    hit = _stored_hit(r, blended, spec["beat"])
                if blended >= min_score:
                    out.append(hit)
            out.sort(key=lambda x: -x["score"])
            took = (_dt.datetime.now() - t0).total_seconds() * 1000
            res = {"query": query, "beat": spec["beat"], "expanded": True,
                   "terms": len(spec["terms"]), "scanned": len(rows),
                   "total": len(out), "took_ms": round(took, 1),
                   "results": out}
            if record:
                self._record(res, paper, date_from, date_to, beat)
            return res

        # Free text has no precomputed score -- there is no vocabulary to have
        # scored against -- so this path still over-fetches and rescores.
        if True:
            words = [w for w in _PUNCT.sub(" ", query).split() if w]
            if not words:
                return self._empty(query, spec)
            flt["$text"] = {"$search": " ".join(words)}
            rows = list(self.articles
                        .find(flt, {"score": {"$meta": "textScore"}})
                        .sort([("score", {"$meta": "textScore"})]).limit(cap))

        out = []
        if rows:
            best = max((r.get("score") or 0) for r in rows) or 1.0
            for r in rows:
                art = {"headline": r.get("headline", ""), "body": r.get("body", "")}
                s = keywords.score(art, spec)
                # The $in path yields no relevance number, so lexical strength
                # is how much of the vocabulary the document actually contains.
                lexs = (min(1.0, len(s["hits"]) / 8.0) if spec["expanded"]
                        else min(1.0, (r.get("score") or 0) / best))
                blended = round(W_LEX * lexs + W_BEAT * s["score"], 3)
                if blended < min_score:
                    continue
                out.append(_hit(r, blended, lexs, s))
            out.sort(key=lambda x: -x["score"])

        took = (_dt.datetime.now() - t0).total_seconds() * 1000
        res = {"query": query, "beat": spec["beat"], "expanded": spec["expanded"],
               "terms": len(spec["terms"]), "scanned": len(rows),
               "total": len(out), "took_ms": round(took, 1),
               "results": out[:limit]}
        if record:
            self._record(res, paper, date_from, date_to, beat)
        return res

    def _record(self, res, paper, date_from, date_to, beat):
        """
        Log the query. Cheap, and it is the only source of truth for what
        people actually search for -- which is what tells you the next beat
        vocabulary to build.
        """
        try:
            self.history.insert_one({
                "query": res["query"], "beat": res["beat"],
                "expanded": res["expanded"], "results": res["total"],
                "took_ms": res["took_ms"],
                "filters": {"paper": paper, "date_from": date_from,
                            "date_to": date_to, "beat": beat},
                "at": _dt.datetime.now()})
        except Exception:
            pass                                   # never fail a search on logging

    def rescore(self, batch: int = 500) -> dict:
        """
        Recompute stored beat scores over the whole corpus.

        The counterpart to precomputing them at ingest: stored scores are only
        as current as the vocabulary that produced them, so every edit to
        keywords.py -- a new term, a weight change, a region added -- leaves
        them stale until this runs. Cheap relative to extraction; it touches no
        model and re-reads no page.
        """
        n = changed = 0
        for r in self.articles.find({}, {"headline": 1, "body": 1, "beat_scores": 1,
                                         "keywords": 1, "beats": 1, "category": 1}):
            hits, beats, scores, category = beats_for(
                {"headline": r.get("headline", ""), "body": r.get("body", "")})
            n += 1
            if (r.get("beat_scores") == scores and r.get("keywords") == hits
                    and r.get("beats") == beats):
                continue
            self.articles.update_one(
                {"_id": r["_id"]},
                {"$set": {"keywords": hits, "beats": beats,
                          "beat_scores": scores, "category": category}})
            changed += 1
        return {"scanned": n, "updated": changed}

    def history_recent(self, limit: int = 30) -> list:
        rows = self.history.find({}, {"_id": 0}).sort("at", DESCENDING).limit(limit)
        return [dict(r, at=r["at"].isoformat()) for r in rows]

    def article(self, uid: str):
        """One article with its page and issue resolved, for the highlight view."""
        a = self.articles.find_one({"uid": uid}, {"_id": 0})
        if not a:
            return None
        pg = self.pages.find_one({"_id": a.get("page_id")}, {"_id": 0})
        ep = self.epapers.find_one({"_id": a.get("epaper_id")}, {"_id": 0})
        a.pop("epaper_id", None)
        a.pop("page_id", None)
        a["updated_at"] = str(a.get("updated_at") or "")
        a["created_at"] = str(a.get("created_at") or "")
        return {"article": a, "page": _clean(pg), "epaper": _clean(ep)}

    @staticmethod
    def _empty(query, spec):
        return {"query": query, "beat": spec["beat"], "expanded": spec["expanded"],
                "terms": len(spec["terms"]), "scanned": 0, "total": 0,
                "took_ms": 0.0, "results": []}

    def stats(self) -> dict:
        agg = lambda col, field: [                                # noqa: E731
            {"key": x["_id"], "n": x["n"]} for x in col.aggregate([
                {"$group": {"_id": "$" + field, "n": {"$sum": 1}}},
                {"$sort": {"n": -1}}, {"$limit": 20}])]
        dates = list(self.articles.aggregate([
            {"$group": {"_id": None, "d0": {"$min": "$date"}, "d1": {"$max": "$date"}}}]))
        return {
            "epapers": self.epapers.count_documents({}),
            "pages": self.pages.count_documents({}),
            "articles": self.articles.count_documents({}),
            "searches": self.history.count_documents({}),
            "by_paper": [{"paper": x["key"], "n": x["n"]}
                         for x in agg(self.articles, "paper")],
            "by_category": [{"category": x["key"] or "(none)", "n": x["n"]}
                            for x in agg(self.articles, "category")],
            "earliest": (dates[0]["d0"] if dates else None),
            "latest": (dates[0]["d1"] if dates else None),
        }


# -- helpers --------------------------------------------------------------

def _clean(d):
    if not d:
        return None
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in d.items() if k != "epaper_id"}


def _stored_hit(r, blended, beat_id):
    """A hit built from precomputed fields -- no scoring work at query time."""
    kws = r.get("keywords") or []
    sc = r.get("beat_scores") or {}
    return {
        "uid": r.get("uid"), "job_id": r.get("job_id"),
        "paper": r.get("paper"), "edition": r.get("edition"),
        "date": r.get("date"), "page_number": r.get("page_number"),
        "headline": r.get("headline", ""), "body": (r.get("body") or "")[:600],
        "byline": r.get("byline", ""), "lang": r.get("lang"),
        "category": r.get("category", ""),
        "bbox": r.get("bbox") or [], "regions": r.get("regions") or [],
        "crop": r.get("crop"), "text_source": r.get("text_source"),
        "score": blended,
        "lexical": round(min(1.0, len(kws) / 8.0), 3),
        "beat_score": blended, "keywords": kws[:12],
        "off_patch": bool(sc.get(beat_id + "__off")),
    }


def _hit(r, blended, lexs, s):
    return {
        "uid": r.get("uid"), "job_id": r.get("job_id"),
        "paper": r.get("paper"), "edition": r.get("edition"),
        "date": r.get("date"), "page_number": r.get("page_number"),
        "headline": r.get("headline", ""), "body": (r.get("body") or "")[:600],
        "byline": r.get("byline", ""), "lang": r.get("lang"),
        "category": r.get("category", ""),
        "bbox": r.get("bbox") or [], "regions": r.get("regions") or [],
        "crop": r.get("crop"), "text_source": r.get("text_source"),
        "score": blended, "lexical": round(lexs, 3),
        "beat_score": s["score"], "keywords": s["hits"][:12],
        "off_patch": s["off_patch"],
    }


def _as_list(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return list(v) if v else []


def _regions_of(a: dict, bbox: list, headline: str) -> list:
    """
    Typed sub-regions of the article.

    Prefers what the grouper actually saw (`region_boxes`, carried out of
    local_backend). Falls back to a single article-level region -- honest about
    having only the union box, rather than inventing a headline/body split that
    was never measured.
    """
    rb = a.get("region_boxes")
    if isinstance(rb, list) and rb:
        return [{"type": r.get("type") or "body_text",
                 "bbox": _as_list(r.get("bbox")),
                 "text": r.get("text") or ""} for r in rb]
    if bbox:
        return [{"type": "article", "bbox": bbox, "text": headline}]
    return []


def _norm_headline(s: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT.sub("", (s or "").lower())).strip()


# Bounding boxes are quantised before hashing so that a few pixels of drift
# between extractions does not mint a new identity. 20px on a 2400px page is
# under 1%, far below the gap between adjacent articles.
BOX_QUANT = 20.0


def uid_for(paper, date, page_number, headline, bbox) -> str:
    """
    Stable identity for one article, keyed on GEOMETRY not text.

    Headline used to come first. That was wrong, and it cost five duplicate
    rows on a thirteen-article page: the same story read by OCR and then by the
    vision model produces "Proud oftaggiven" and "Proud of tag given", two
    different keys for one article at one position. Text is what varies between
    extraction methods; the box is what does not.

    Headline remains the fallback for articles that have no box at all --
    publisher-supplied text from Summit, where there is no page geometry.
    Every component must be populated: an empty `paper` once produced a second
    copy of every article because the key silently changed.
    """
    if bbox and len(bbox) == 4:
        key = "b:" + ",".join(str(int(round(float(v) / BOX_QUANT))) for v in bbox)
    else:
        key = "h:" + _norm_headline(headline)
    raw = "%s|%s|%s|%s" % (paper or "", date or "", page_number or 0, key)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def article_id_for(paper, date, page_number, uid) -> str:
    """
    store.py's human-readable id, made stable.

    Same shape as store.article_id ("tribune-delhi-2026-08-17-p01-...") but
    keyed on uid instead of a sequence number, so re-extracting a page yields
    the same id and upserts rather than colliding on the unique index.
    """
    slug = "".join(c if c.isalnum() else "-" for c in (paper or "x").lower()).strip("-")
    return "%s-%s-p%s-%s" % (slug, date or "", str(page_number or 0).zfill(2), uid[:8])


def detect_lang(text: str) -> str:
    return "hi" if _DEVANAGARI.search(text or "") else "en"


def beats_for(article: dict) -> tuple:
    """
    Which beats claim this article, the terms they found, and the final scores.

    The score is computed HERE, at write time, not at query time. For a
    resolved beat the score is a property of the article alone -- the query
    contributes nothing beyond selecting which vocabulary to use -- so
    recomputing it per query was pure waste. Measured at 5,000 articles:
    350 ms of a 367 ms query was 292 regexes re-run over 200 candidates, while
    Mongo itself took 3.6 ms.

    An article is tagged only with beats whose patch it is actually on. That
    filter is what keeps `beats` worth indexing now that there are 36 of them:
    the policing vocabulary is shared, so without it every police story in the
    country would be tagged with all 36 forces and the index would select
    nothing. A story naming Ludhiana is Punjab's; a story naming no place at
    all is genuinely nobody's in particular and is tagged with all of them,
    which is the honest answer rather than a guess.

    Staleness is the trade. These numbers are only as current as the
    vocabulary that produced them, so editing a beat -- in keywords.py or from
    the backend -- means re-running /api/corpus/rescore. That is the right side
    of the trade: vocabularies change rarely, queries happen constantly.
    """
    hits, beats, scores = [], [], {}
    best, best_score = "", -1.0
    for beat_id, s in keywords.score_all(article).items():
        if not s["hits"] or s["off_patch"]:
            continue
        beats.append(beat_id)
        hits += s["hits"]
        lexs = min(1.0, len(s["hits"]) / 8.0)
        blended = round(W_LEX * lexs + W_BEAT * s["score"], 3)
        scores[beat_id] = blended
        scores[beat_id + "__off"] = False
        # Which beat OWNS the article, for the `category` field. Previously the
        # first beat that matched, which was fine with one beat and arbitrary
        # with 36 -- every placeless police story would have been filed under
        # whichever force happened to lead the table.
        if blended > best_score or (blended == best_score and
                                    beat_id < (best or "￿")):
            best, best_score = beat_id, blended
    return sorted(set(hits)), beats, scores, best
