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

Two fields answer two different questions about an article, and they are not
interchangeable:

    primary_beat    WHERE it belongs -- the beat whose vocabulary claims it
                    most strongly. Geography and desk. Computed here, free.
    category        WHAT it is about -- the topical label. Assigned by the
                    classifier, and empty until that stage runs.

Both were once written under the name `category`, so a corpus of state-police
beats answered every question about subject matter with "delhi-police". Keeping
them apart is what makes browsing by topic possible at all.

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

from . import catdata, categories, config, fragments, keywords
from . import query as _query

# Blend of the two rankers. Mongo knows which documents contain the words; the
# beat vocabulary knows which of them are on the beat, and carries the region
# gate. Weighted toward the beat because that is the half that understands what
# the terms mean.
W_LEX, W_BEAT = 0.40, 0.60

# Per residual word found, added to a beat hit's score. Small on purpose: the
# beat vocabulary is still what decides relevance, and one extra word the
# reader happened to type should reorder the list, not rewrite it.
RESIDUAL_BONUS = 0.12

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
        self.articles.create_index([("primary_beat", ASCENDING)])
        # Multikey: one entry per tag per article. This is what makes
        # narrowing a topic to a specific ("crime" -> "otp fraud") an index
        # lookup rather than a scan of everything filed under the topic.
        self.articles.create_index([("tags", ASCENDING)])
        # The second level. Indexed on its own and beneath its parent, because
        # both questions get asked: "every murder story" and "the murders
        # inside today's crime".
        self.articles.create_index([("subcategory", ASCENDING)])
        self.articles.create_index([("category", ASCENDING),
                                    ("subcategory", ASCENDING)])
        # The browse path's only query: one day's articles, grouped by topic or
        # filtered to one. Compound and in this order because `date` is always
        # an equality match and `category` is either grouped or matched exactly,
        # so Mongo can serve both the facet counts and the article list from
        # this one index without touching a document.
        self.articles.create_index([("date", DESCENDING), ("category", ASCENDING)])
        # The second level. Stored on the article alongside its parent rather
        # than resolved through the registry at query time: selecting "crime"
        # has to match every story under it, and expanding a heading into its
        # children on every request would turn one indexed equality into an
        # $in over a list that changes as the tree grows.
        self.articles.create_index([("subcategory", ASCENDING)])
        self.articles.create_index([("date", DESCENDING),
                                    ("subcategory", ASCENDING)])
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
            # The same test, for the case that one misses. A dateline BECOMES
            # the block's headline, so it has one and passes the check above;
            # this is what actually keeps "मुंबई, 16 अणस्त (एजेंसी)" out of
            # the archive. Repeated here rather than trusted to the caller
            # because every path into the corpus needs it -- the topic job and
            # the SQLite backfill do not go through run_job.
            if fragments.reason(a):
                counts["skipped"] += 1
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
            hits, beats, beat_scores, primary_beat = beats_for(
                {"headline": headline, "body": body})

            # The label the classifier produced, filed under whatever it has
            # been merged into. Two states are deliberately distinguishable:
            # "" means nobody has classified this article yet, while
            # catdata.UNKNOWN means the classifier ran and could not label it.
            # Collapsing them would make a model outage indistinguishable from
            # a backlog, and only one of those is fixed by waiting.
            topic = (a.get("category") or "").strip()
            if topic:
                topic = categories.file_as(topic, a.get("raw_category") or "")
            # The second level is recorded too, under its heading. Without
            # this the registry only ever sees parents, `unplaced()` is
            # permanently empty and there is nothing for assign_parents to
            # place -- the counts that decide which labels deserve a decision
            # are exactly the ones that would be missing.
            sub = categories.file_child(a.get("subcategory") or "", topic)

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
                # WHERE the story belongs: the beat whose vocabulary claims it
                # most strongly. Geography and desk, not subject matter.
                "primary_beat": primary_beat,
                # WHAT the story is about, as the classifier labelled it.
                # These two lived under the name `category` at once, which is
                # why a corpus full of police beats answered every question
                # about topic with "delhi-police".
                "category": topic,
                "subcategory": catdata.normalise(a.get("subcategory")) or "",
                # The specific kind within that heading -- "murder" under
                # "crime". Free where `category` is controlled, which is the
                # whole shape: a stable picker at the top, full coverage below.
                "subcategory": sub,
                # The classifier's own wording, before normalisation and
                # aliasing. Kept so a bad alias can be told from a bad label.
                "raw_category": a.get("raw_category") or "",
                "tags": _as_tags(a.get("tags")),
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
               beat=None, category=None, subcategory=None, tag=None,
               limit: int = 50, exclude=None, proximity=None, weekdays=None,
               min_score: float = 0.0, record: bool = True) -> dict:
        """
        Hybrid retrieval over everything ever extracted. No model involved.

        `exclude`, `proximity` and `weekdays` narrow the result the way the
        Topic Search form offers. Exclusions and weekdays are pushed into the
        query so the database does the discarding; proximity is applied to the
        rows that come back, because it depends on where words sit relative to
        each other and there is no index that answers that.
        """
        t0 = _dt.datetime.now()
        spec = keywords.expand(query)

        drop = _query.parse_terms(exclude)
        days = _query.parse_weekdays(weekdays)
        window = _query.window_for(proximity)

        flt = {}
        flt.update(_query.mongo_exclusion(drop))
        flt.update(_query.mongo_weekdays(days))
        if paper:
            flt["paper"] = paper
        if beat:
            flt["beats"] = beat
        if category:
            # Normalised on the way in, so a reader arriving with the label as
            # it is displayed ("Cybercrime") matches what is stored.
            flt["category"] = catdata.normalise(category) or category
        if tag:
            flt["tags"] = catdata.normalise(tag) or tag
        if date_from or date_to:
            flt["date"] = {}
            if date_from:
                flt["date"]["$gte"] = date_from
            if date_to:
                flt["date"]["$lte"] = date_to

        # A force picked in the dropdown outranks one inferred from the words
        # typed: the first is a statement of intent, the second is a guess made
        # by matching an alias anywhere in the query. The guess used to
        # overwrite it, so choosing a force did nothing at all whenever the
        # query happened to name one -- picking "Jharkhand Police" and typing
        # "delhi police" returned Delhi's articles and silently dropped
        # Jharkhand's.
        active = beat or spec["beat"]

        def near_enough(row) -> bool:
            """
            Whether this row satisfies the proximity setting.

            Measured against the query as the reader typed it, not against the
            expanded vocabulary. Asking for "Delhi Police" within five words
            is a statement about those two words; applying it to all 24 terms
            the beat expands to would demand a coincidence nothing satisfies.
            """
            if window is None:
                return True
            text = row.get("full_text") or "%s %s" % (row.get("headline") or "",
                                                      row.get("body") or "")
            return _query.within(text, query, window)

        cap = max(limit * 4, 200)
        if active:
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
            field = "beat_scores.%s" % active
            # dict(flt, ...) rather than flt["beats"] = ...: the filter has to
            # survive this pass unmodified, because the text fallback below
            # reuses it and must not inherit a beat restriction that just
            # failed to match anything.
            # Over-fetch rather than taking `limit` straight from Mongo. The
            # final order is not the stored score alone -- geography tiers
            # above it, and the reader's residual words adjust it -- so
            # truncating in the database would drop rows that belong at the
            # top before either had been applied.
            rows = list(self.articles.find(dict(flt, beats=active))
                        .sort(field, DESCENDING).limit(cap))
            # With a force chosen from the dropdown the query text was never
            # consumed by an alias, so the whole of it is residual -- otherwise
            # picking a force and typing "election" would rank by beat score
            # alone and ignore the word entirely.
            resid = (spec.get("residual") or []) if spec["expanded"] \
                else (spec.get("terms") or [])
            out = []
            for r in rows:
                if not near_enough(r):
                    continue
                sc = (r.get("beat_scores") or {})
                blended = sc.get(active)
                if blended is None:
                    # Written before scores were precomputed: fall back rather
                    # than drop the row, and let a backfill fix it properly.
                    s = keywords.score({"headline": r.get("headline", ""),
                                        "body": r.get("body", "")}, spec)
                    lexs = min(1.0, len(s["hits"]) / 8.0)
                    blended = round(W_LEX * lexs + W_BEAT * s["score"], 3)
                    hit = _hit(r, blended, lexs, s)
                else:
                    hit = _stored_hit(r, blended, active)
                # The reader's own words, which the beat name did not consume.
                # They RANK rather than select: an article missing the extra
                # word is still on the beat and still worth returning, just
                # lower down. Requiring it would throw away the recall the
                # vocabulary exists to provide.
                if resid:
                    blended = round(min(1.0, blended + RESIDUAL_BONUS *
                                        _residual_hits(r, resid)), 3)
                    hit["score"] = blended
                if blended >= min_score:
                    out.append(hit)
            if out:
                # Geography first, then score. A story that names this force's
                # own patch is its story; a placeless one merely could be, and
                # is tagged to all 36 forces precisely because there is nothing
                # to tell them apart. Ranking the two on a single number let
                # the placeless story win whenever its vocabulary was heavier
                # -- "Speed up Ram Temple donation probe, SC tells SIT" came
                # back above "Jharkhand scraps exams" for the query
                # "jharkhand", 0.472 against 0.43, and no amount of tuning the
                # weight fixes that, because the two are not the same kind of
                # evidence. Within each tier the score still orders.
                out.sort(key=lambda x: (0 if x.get("on_patch") else 1,
                                        -x["score"]))
                took = (_dt.datetime.now() - t0).total_seconds() * 1000
                res = {"query": query, "beat": active,
                       "expanded": spec["expanded"],
                       "terms": len(spec["terms"]), "scanned": len(rows),
                       "loose": False,
                       "total": len(out), "took_ms": round(took, 1),
                       "results": out[:limit]}
                if record:
                    self._record(res, paper, date_from, date_to, beat)
                return res
            # Nothing carries this beat's tag, so fall through to the text
            # index rather than answering "nothing found".
            #
            # Tagging happens at ingest, which means the set of articles a beat
            # query can ever reach was fixed before the question was asked.
            # Anything indexed before a vocabulary change, or whose headline
            # OCR mangled, carries no tag and was unreachable by that route
            # forever -- while plainly containing the word the reader typed.
            # The words on the page are still there to be searched, so search
            # them instead of reporting an empty corpus.

        # Free text has no precomputed score -- there is no vocabulary to have
        # scored against -- so this path still over-fetches and rescores.
        words = [w for w in _PUNCT.sub(" ", query).split() if w]
        if not words:
            return self._empty(query, spec)
        rows = list(self.articles
                    .find(dict(flt, **{"$text": {"$search": " ".join(words)}}),
                          {"score": {"$meta": "textScore"}})
                    .sort([("score", {"$meta": "textScore"})]).limit(cap))

        # Recall net. The text index carries default_language "none", which is
        # right for a bilingual corpus and means Mongo does no stemming at all:
        # `case` and `cases` are unrelated tokens, so a story headlined "CBI
        # charges husband" is invisible to a search for "charge". The article
        # is in the database and the search says nothing is there, which is the
        # worst answer available because it looks like an empty corpus.
        #
        # The module docstring claims the keyword layer's prefix matching
        # covers morphology. It does not cover this path: that layer only
        # rescores rows $text already returned, so it cannot rescue a query
        # that matched nothing, and its prefix rule applies to Devanagari only.
        #
        # So: exact and indexed first, unchanged and just as fast. Only when it
        # comes back empty does this scan with a trimmed prefix. A query that
        # already found something never pays for it.
        # Run the loose pass when the exact one found nothing, and ALSO when
        # the reader typed an inflected word -- those are the two ways this
        # index loses documents, and only the first looks like a failure.
        #
        # The second is quieter and worse. "court" returns three articles and
        # "courts" returns two, because the third says only the singular; the
        # reader sees a plausible answer and never learns it was short. An
        # empty result at least invites a retry.
        #
        # Only inflected queries pay for the scan. "court" stems to itself, so
        # it takes the indexed path alone; "courts" stems to "court" and gets
        # both. That is also what makes the pair agree, which is the point --
        # a reader should not have to guess which form the paper used.
        stems = [_stem(w) for w in words]
        loose = False
        if not rows or any(s != w.lower() for s, w in zip(stems, words)):
            wider = list(self.articles.find(
                dict(flt, **{"$or": [_loose_clause(w) for w in words]})
            ).limit(cap))
            if not rows:
                rows, loose = wider, bool(wider)
            else:
                seen = {r.get("uid") for r in rows}
                extra = [r for r in wider if r.get("uid") not in seen]
                if extra:
                    # No textScore on these, so they rank below the exact
                    # matches once rescored. Looser evidence, lower placing.
                    rows += extra
                    loose = True

        out = []
        if rows:
            best = max((r.get("score") or 0) for r in rows) or 1.0
            for r in rows:
                if not near_enough(r):
                    continue
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
               # Whether the exact index found nothing and the prefix fallback
               # answered instead. Worth surfacing: those results are looser by
               # construction, and a reader deserves to know which pass they
               # came from rather than wondering why the ranking changed.
               "loose": loose,
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
                                         "keywords": 1, "beats": 1,
                                         "primary_beat": 1}):
            hits, beats, scores, primary_beat = beats_for(
                {"headline": r.get("headline", ""), "body": r.get("body", "")})
            n += 1
            if (r.get("beat_scores") == scores and r.get("keywords") == hits
                    and r.get("beats") == beats
                    and r.get("primary_beat") == primary_beat):
                continue
            # `category` is deliberately absent: it holds the topical label now,
            # which no vocabulary change can affect. Rescoring must not touch it.
            self.articles.update_one(
                {"_id": r["_id"]},
                {"$set": {"keywords": hits, "beats": beats,
                          "beat_scores": scores, "primary_beat": primary_beat}})
            changed += 1
        return {"scanned": n, "updated": changed}

    def reclassify(self, *, only_unlabelled: bool = True, limit: int = 0,
                   date=None, paper=None, batch: int = 25,
                   progress=None) -> dict:
        """
        Assign topics to articles already in the archive.

        Three jobs, one method. It fills in articles that were extracted before
        the classifier existed; it re-labels everything after a prompt change;
        and it is the repair when a model outage left a night's work blank.

        `only_unlabelled=True` by default, because that is the cheap and
        common case -- a full re-label of a large archive is minutes of GPU
        time per thousand articles and should be asked for explicitly.

        Imported here rather than at module scope: classify pulls in the model
        client, and the corpus must stay importable on a machine with no model
        server at all. Search, browse and ingest never reach this line.
        """
        from . import classify as _classify

        flt = self._scope(date=date, paper=paper)
        # A topic somebody set by hand is the last word. A bulk relabel that
        # overwrote it would undo an afternoon of corrections on the next
        # overnight run, and the person who made them would not find out until
        # they went looking for an article they had already fixed.
        flt["category_source"] = {"$ne": "human"}
        if only_unlabelled:
            # "" is never classified. catdata.UNKNOWN is classified and
            # unlabelable, and is deliberately NOT included -- re-asking a
            # model that already declined costs time and rarely changes.
            flt["category"] = {"$in": ["", None]}

        total = self.articles.count_documents(flt)
        if limit:
            total = min(total, int(limit))
        done = {"scanned": 0, "labelled": 0, "failed": 0, "topics": {}}
        if not total:
            return done

        cursor = self.articles.find(
            flt, {"_id": 1, "headline": 1, "body": 1}).limit(int(limit) or 0)

        # Batched so a long run writes as it goes. An interrupted reclassify
        # then keeps everything it had already labelled instead of discarding
        # an hour of GPU time, and re-running it resumes rather than restarts.
        #
        # Small on purpose, and it has to be smaller than any archive worth
        # running this on. Set to 200 first, which meant a 127-article corpus
        # accumulated one chunk, wrote nothing for twenty minutes and would
        # have lost all of it on a Ctrl-C -- the exact failure the batching
        # was added to prevent. A write every 25 costs one extra round trip
        # per 25 articles against a stage that already spends ~90 s on them.
        chunk = []
        for row in cursor:
            chunk.append(row)
            if len(chunk) >= batch:
                self._reclassify_chunk(chunk, done, _classify)
                chunk = []
                if progress:
                    progress(done["scanned"], total)
        if chunk:
            self._reclassify_chunk(chunk, done, _classify)
            if progress:
                progress(done["scanned"], total)

        done["topics"] = dict(sorted(done["topics"].items(),
                                     key=lambda kv: -kv[1]))
        return done

    def _reclassify_chunk(self, rows, done, _classify):
        for row, res in zip(rows, _classify.classify(rows)):
            done["scanned"] += 1
            topic = res["category"]
            if res.get("classify_error"):
                done["failed"] += 1
            else:
                done["labelled"] += 1
            filed = categories.file_as(topic, res["raw_category"]) \
                if topic else ""
            done["topics"][filed or "(none)"] = \
                done["topics"].get(filed or "(none)", 0) + 1
            self.articles.update_one(
                {"_id": row["_id"]},
                {"$set": {"category": filed,
                          "raw_category": res["raw_category"],
                          "tags": res["tags"],
                          "updated_at": _dt.datetime.now()}})

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

    def set_topic(self, uid: str, *, category=None, tags=None) -> dict:
        """
        Correct one article's topic by hand. Returns the updated article.

        A person overruling the classifier is the last word, and the stored
        `category_source` says so. Everything that relabels in bulk --
        reclassify after a prompt change, a re-ingest of the same page -- reads
        that field and leaves human answers alone. Without it the first
        overnight run would quietly undo an afternoon of corrections, and
        nobody would notice until they went looking for an article they had
        already fixed.

        Passing `category=""` clears the label back to unclassified, which is
        also a decision a person is allowed to make.
        """
        doc = self.articles.find_one({"uid": uid}, {"_id": 1})
        if not doc:
            raise KeyError(uid)

        upd = {"updated_at": _dt.datetime.now()}
        if category is not None:
            label = (category or "").strip()
            if label:
                # Filed through the registry like any other label, so a
                # hand-typed topic gets the same normalisation, the same merge
                # redirects, and appears in the picker beside the rest.
                filed = categories.file_as(label, label)
                upd["category"] = filed
                upd["raw_category"] = label
                upd["category_source"] = "human"
            else:
                upd["category"] = ""
                upd["raw_category"] = ""
                upd["category_source"] = "human"
        if tags is not None:
            upd["tags"] = catdata.normalise_tags(tags)
            upd["tags_source"] = "human"

        self.articles.update_one({"_id": doc["_id"]}, {"$set": upd})
        return self.article(uid)

    @staticmethod
    def _empty(query, spec):
        return {"query": query, "beat": spec["beat"], "expanded": spec["expanded"],
                "terms": len(spec["terms"]), "scanned": 0, "total": 0,
                "loose": False, "took_ms": 0.0, "results": []}

    # -- browse ------------------------------------------------------------
    #
    # The reader path that is not a search. Someone reviewing yesterday does
    # not have a query -- they have a date, and they want to be shown what
    # subjects appeared before choosing one. Nothing here scores, expands or
    # calls a model: every one of these is an indexed read, which is what the
    # (date, category) compound index exists for.

    @staticmethod
    def _scope(date=None, paper=None, date_from=None, date_to=None) -> dict:
        """The filter shared by facets, browse and dates. One definition, so a
        facet count can never describe a different set than the list under it."""
        flt = {}
        if date:
            flt["date"] = date
        elif date_from or date_to:
            flt["date"] = {}
            if date_from:
                flt["date"]["$gte"] = date_from
            if date_to:
                flt["date"]["$lte"] = date_to
        if paper:
            flt["paper"] = paper
        return flt

    def has_page(self, paper, date, page_number) -> bool:
        """
        Whether this page has already been extracted.

        What makes a full-edition run resumable. Reading a page costs a minute
        of GPU time, so a job that died on page 19 of 24 must not start again
        at page 1 -- and a nightly schedule that fires twice must not pay for
        the same edition twice.

        Keyed on articles rather than the `pages` collection: a page row is
        written before its articles are, so a crash between the two would look
        like a finished page holding nothing.
        """
        return self.articles.count_documents(
            {"paper": paper or "", "date": date or "",
             "page_number": int(page_number or 0)}, limit=1) > 0

    def facets(self, *, date=None, paper=None, date_from=None, date_to=None,
               category=None, limit: int = 40, tag_limit: int = 25) -> dict:
        """
        Which categories appeared, and how many articles each holds.

        The screen between picking a date and seeing the clips. Unlabelled
        articles are reported under their own entry rather than dropped: a day
        that has not been classified yet must look different from a day with
        no crime in it, and silently omitting them makes those identical.

        Passing `category` scopes the TAGS to that category while leaving the
        category counts alone. That one difference is what turns a flat row of
        keywords into a hierarchy: the tags inside "crime" are murder, robbery
        and snatching, and they are exactly the sub-categories of crime. The
        counts stay unscoped so the other categories remain selectable -- a
        reader who picks crime has narrowed what they are looking at, not
        thrown away the rest of the day.
        """
        flt = self._scope(date, paper, date_from, date_to)
        cats = list(self.articles.aggregate([
            {"$match": flt},
            {"$group": {"_id": "$category", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": int(limit)}]))

        scoped = dict(flt)
        if category:
            scoped["category"] = catdata.normalise(category) or category
        tags = list(self.articles.aggregate([
            {"$match": dict(scoped, tags={"$nin": [None, []]})},
            {"$unwind": "$tags"},
            {"$group": {"_id": "$tags", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": int(tag_limit)}]))

        # The second level, counted per (parent, child) so a heading's chips
        # and the articles behind them cannot disagree.
        subs = list(self.articles.aggregate([
            {"$match": dict(flt, subcategory={"$nin": ["", None]})},
            {"$group": {"_id": {"c": "$category", "s": "$subcategory"},
                        "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}]))
        kids = {}
        for x in subs:
            kids.setdefault(x["_id"]["c"] or "", []).append(
                {"subcategory": x["_id"]["s"],
                 "label": catdata.display(x["_id"]["s"]), "n": x["n"]})

        out, unlabelled = [], 0
        for x in cats:
            key = x["_id"] or ""
            if not key:
                unlabelled = x["n"]
                continue
            out.append({"category": key, "label": catdata.display(key),
                        "n": x["n"], "children": kids.get(key, [])})
        return {
            "date": date, "paper": paper, "category": category,
            "total": self.articles.count_documents(flt),
            # Articles nobody has classified. Named separately from
            # catdata.UNKNOWN, which means the classifier ran and failed --
            # one is a backlog, the other is a quality problem.
            "unlabelled": unlabelled,
            "categories": out,
            "tags": [{"tag": t["_id"], "n": t["n"]} for t in tags if t["_id"]],
        }

    def tree(self, *, date=None, paper=None, date_from=None, date_to=None,
             limit: int = 30, sub_limit: int = 12, min_sub: int = 1) -> dict:
        """
        The two levels, in one call: headings with what sits under each.

        Nothing declares this hierarchy. The classifier gives every article a
        heading from a controlled list and a subcategory in its own words, so
        the tree is a co-occurrence count over data that already exists and it
        rebuilds itself every night. A subject the papers start covering shows
        up the night it appears; one they stop covering fades out on its own.

        A stored parent-child table would have to be written by somebody, kept
        in step with a vocabulary that changes weekly, and would be wrong the
        moment it was not -- which is exactly the maintenance burden the free
        second level exists to avoid. Deriving it gets the same shape for free.

        Articles with no subcategory are reported per heading as `direct`
        rather than dropped. A heading whose children sum to less than its own
        count is not a bug, and hiding the difference makes it look like one.
        """
        flt = self._scope(date, paper, date_from, date_to)
        base = dict(flt, category={"$nin": ["", None]})

        pairs = list(self.articles.aggregate([
            {"$match": base},
            {"$group": {"_id": {"c": "$category", "s": "$subcategory"},
                        "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ]))
        totals = {x["_id"]: x["n"] for x in self.articles.aggregate([
            {"$match": base},
            {"$group": {"_id": "$category", "n": {"$sum": 1}}}])}

        cats = {}
        for c, n in totals.items():
            cats[c] = {"category": c, "label": catdata.display(c), "n": n,
                       "direct": 0, "children": []}
        for r in pairs:
            c, sub = r["_id"]["c"], (r["_id"].get("s") or "")
            node = cats.get(c)
            if not node:
                continue
            if sub:
                node["children"].append({"subcategory": sub,
                                         "label": catdata.display(sub),
                                         "n": r["n"]})
            else:
                # Filed under the heading with no specific kind named.
                node["direct"] += r["n"]

        out = sorted(cats.values(), key=lambda c: -c["n"])[:int(limit)]
        for node in out:
            kids = [k for k in node["children"] if k["n"] >= int(min_sub)]
            node["children"] = sorted(kids, key=lambda k: -k["n"])[:int(sub_limit)]
        return {"date": date, "paper": paper,
                "parents": list(catdata.PARENTS), "categories": out}

    def browse(self, *, date=None, category=None, subcategory=None,
               paper=None, tag=None, date_from=None, date_to=None,
               page: int = 1, per_page: int = 50) -> dict:
        """
        One day's articles, or one topic within it, in the order they were
        printed. No query, no ranking, no model.

        Sorted by page then by vertical position, which is reading order --
        the order somebody turning the paper would meet them in. Sorting by
        relevance here would be answering a question the reader did not ask.
        """
        flt = self._scope(date, paper, date_from, date_to)
        if category:
            flt["category"] = catdata.normalise(category) or category
        if subcategory:
            flt["subcategory"] = catdata.normalise(subcategory) or subcategory
        if tag:
            flt["tags"] = catdata.normalise(tag) or tag

        per_page = max(1, min(int(per_page), 200))
        page = max(1, int(page))
        total = self.articles.count_documents(flt)
        rows = list(self.articles.find(flt)
                    .sort([("page_number", ASCENDING), ("bbox.1", ASCENDING)])
                    .skip((page - 1) * per_page).limit(per_page))
        return {
            "date": date, "category": category, "subcategory": subcategory,
            "tag": tag, "paper": paper, "total": total, "page": page, "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
            "results": [_plain_hit(r) for r in rows],
        }

    def dates(self, *, paper=None, limit: int = 120) -> list:
        """
        Which days the archive actually holds, newest first, with counts.

        So the date picker can grey out days with nothing in them instead of
        letting a reader select one and be shown an empty screen they cannot
        distinguish from a failure.
        """
        flt = self._scope(paper=paper)
        return [{"date": x["_id"], "n": x["n"], "labelled": x["labelled"]}
                for x in self.articles.aggregate([
                    {"$match": dict(flt, date={"$nin": ["", None]})},
                    {"$group": {"_id": "$date", "n": {"$sum": 1},
                                "labelled": {"$sum": {"$cond": [
                                    {"$in": ["$category", ["", None]]}, 0, 1]}}}},
                    {"$sort": {"_id": -1}}, {"$limit": int(limit)}])]

    def by_paper(self, date: str) -> list:
        """
        One day's haul, split by title. What the Daily Reader lists.

        The counts come from the articles themselves rather than from what the
        collection run reported, because the two can disagree and only one of
        them is what a reader can actually open: a run that fetched fourteen
        pages and failed to read any of them is a success in the job log and an
        empty day on the page.
        """
        rows = list(self.articles.aggregate([
            {"$match": {"date": date}},
            {"$group": {
                "_id": {"paper": "$paper", "edition": "$edition"},
                "n": {"$sum": 1},
                "pages": {"$addToSet": "$page_number"},
                "labelled": {"$sum": {"$cond": [
                    {"$in": ["$category", ["", None, catdata.UNKNOWN]]}, 0, 1]}},
                "categories": {"$addToSet": "$category"}}},
            {"$sort": {"n": DESCENDING}}]))
        out = []
        for r in rows:
            cats = sorted(c for c in (r.get("categories") or [])
                          if c and c != catdata.UNKNOWN)
            out.append({
                "paper": r["_id"].get("paper") or "",
                "edition": r["_id"].get("edition") or "",
                "n": r["n"], "labelled": r["labelled"],
                "pages": sorted(p for p in (r.get("pages") or []) if p),
                "categories": cats[:8]})
        return out

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
            # Both are reported, because they answer different questions and
            # the frontend has always asked for `by_beat`. It used to receive
            # `by_category` holding beat ids under the wrong key, so the panel
            # rendered nothing at all on the success path.
            "by_beat": [{"beat": x["key"] or "(none)", "n": x["n"]}
                        for x in agg(self.articles, "primary_beat")],
            "by_category": [{"category": x["key"] or "(unlabelled)", "n": x["n"]}
                            for x in agg(self.articles, "category")],
            "earliest": (dates[0]["d0"] if dates else None),
            "latest": (dates[0]["d1"] if dates else None),
        }


# -- helpers --------------------------------------------------------------

# Only ASCII words are trimmed. Devanagari inflects by extending the stem
# (गिरफ्तार -> गिरफ्तारी), which the prefix match already covers, and English
# suffix rules mean nothing there.
_LATIN = re.compile(r"^[A-Za-z][A-Za-z'-]*$")

# What is left after trimming must still be long enough to be a word rather
# than a fragment: "filed" would otherwise become "fil", which prefix-matches
# "film" and "filter". Leaving such a word untrimmed costs nothing, because
# the opposite query ("file") still reaches it by prefix.
MIN_STEM = 4


def _stem(word: str) -> str:
    """
    "cases" -> "case", "charges" -> "charg", "arrested" -> "arrest".

    Crude on purpose, and it does not need to be better. The result is only
    ever used as a regex PREFIX, so trimming a little too much widens the net
    while trimming too little merely leaves the exact behaviour in place --
    neither can return a wrong article, only more or fewer of them.

    Both directions are covered by the pairing of trimming and prefixing:
    "cases" is trimmed to "case" and finds "case", while "case" is left alone
    and finds "cases" by prefix. That is what makes the singular and the
    plural interchangeable, which is what a reader expects of a search box.
    """
    if not _LATIN.match(word):
        return word.lower()
    low = word.lower()
    if low.endswith("ies") and len(low) > 4:
        return low[:-3] + "y"
    for suffix in ("ing", "es", "ed", "s"):
        if low.endswith(suffix) and len(low) - len(suffix) >= MIN_STEM:
            return low[:-len(suffix)]
    return low


def _residual_hits(row: dict, resid: list) -> int:
    """How many of the reader's own extra words this article actually contains."""
    hay = ("%s %s" % (row.get("headline") or "", row.get("body") or "")).lower()
    return sum(1 for w in resid if w in hay)


def _loose_clause(word: str) -> dict:
    """
    One word as a word-initial prefix match over `full_text`.

    The left edge is anchored so a term cannot match from inside a longer
    word -- "case" must not hit "staircase". The class is spelled out rather
    than using \\b because Mongo's \\w is ASCII, and an explicit set is one
    less thing to reason about on a bilingual corpus.
    """
    return {"full_text": {"$regex": r"(?<![A-Za-z0-9_])" + re.escape(_stem(word)),
                          "$options": "i"}}


def _clean(d):
    if not d:
        return None
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in d.items() if k != "epaper_id"}


def _base_hit(r) -> dict:
    """
    The fields every result carries, however it was found.

    One definition rather than three. The three builders below used to repeat
    this block, and a field added to one and forgotten in another is exactly
    how a result from the browse path ends up missing something the search
    path returns -- the frontend then renders one and not the other, with
    nothing in either to say why.
    """
    return {
        "uid": r.get("uid"), "job_id": r.get("job_id"),
        "paper": r.get("paper"), "edition": r.get("edition"),
        "date": r.get("date"), "page_number": r.get("page_number"),
        "headline": r.get("headline", ""), "body": (r.get("body") or "")[:600],
        "byline": r.get("byline", ""), "lang": r.get("lang"),
        "category": r.get("category", ""),
        "subcategory": r.get("subcategory", ""),
        "primary_beat": r.get("primary_beat", ""),
        "tags": r.get("tags") or [],
        "bbox": r.get("bbox") or [], "regions": r.get("regions") or [],
        "crop": r.get("crop"), "text_source": r.get("text_source"),
    }


def _stored_hit(r, blended, beat_id):
    """A hit built from precomputed fields -- no scoring work at query time."""
    kws = r.get("keywords") or []
    sc = r.get("beat_scores") or {}
    return dict(_base_hit(r), **{
        "score": blended,
        "lexical": round(min(1.0, len(kws) / 8.0), 3),
        "beat_score": blended, "keywords": kws[:12],
        "off_patch": bool(sc.get(beat_id + "__off")),
        "on_patch": bool(sc.get(beat_id + "__here")),
    })


def _hit(r, blended, lexs, s):
    return dict(_base_hit(r), **{
        "score": blended, "lexical": round(lexs, 3),
        "beat_score": s["score"], "keywords": s["hits"][:12],
        "off_patch": s["off_patch"],
        "on_patch": bool(s.get("in_region")),
    })


def _plain_hit(r) -> dict:
    """
    A browse result. Nothing was ranked, so the scores are reported as zero
    rather than omitted -- the reader is looking at a whole day's articles in
    page order, and a score would be a number with no question behind it.
    """
    return dict(_base_hit(r), **{
        "score": 0.0, "lexical": 0.0, "beat_score": 0.0,
        "keywords": (r.get("keywords") or [])[:12],
        "off_patch": False, "on_patch": False,
    })


def _as_tags(v) -> list:
    """
    The classifier's keyword list, cleaned again on the way in.

    Re-normalised rather than trusted, because ingest is also reachable from
    the backfill path, where the values came out of SQLite and were never
    near catdata. Cheap, and it keeps one spelling rule over the whole
    collection whatever route a document took to get here.
    """
    return catdata.normalise_tags(v)


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
        # Whether the story names this force's own patch, kept alongside the
        # score rather than folded into it. Search ranks on it as a TIER: a
        # story naming Jharkhand belongs to Jharkhand Police more surely than
        # a placeless story does, however much policing vocabulary the latter
        # carries, and no weighting of the two on one scale expresses that.
        scores[beat_id + "__here"] = bool(s["in_region"])
        # Which beat OWNS the article, for the `primary_beat` field. This used
        # to be written as `category`, which made a geographic answer stand in
        # for a topical one. Previously the
        # first beat that matched, which was fine with one beat and arbitrary
        # with 36 -- every placeless police story would have been filed under
        # whichever force happened to lead the table.
        if blended > best_score or (blended == best_score and
                                    beat_id < (best or "￿")):
            best, best_score = beat_id, blended
    return sorted(set(hits)), beats, scores, best
