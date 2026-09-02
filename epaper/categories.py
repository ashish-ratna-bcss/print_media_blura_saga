"""
The topic list in MongoDB: what has been seen, how often, and what is really
the same thing as what else.

epaper/catdata.py folds away the variants that carry no meaning. This module
is the layer for the ones that do -- where two entries are genuinely different
words for one subject and only the frequency counts can show it. The shape is
deliberately the same as epaper/beats.py, because the problem is the same one:
a vocabulary that has to be editable at runtime, by somebody who is not
deploying code, without losing what is already filed.

One collection:

    categories   one row per topic ever seen. Its id (the normalised label),
                 how many articles carry it, when it first and last appeared,
                 and -- if it has been merged away -- which entry it now
                 points at.

Nothing is invented here. Rows appear because the classifier produced them,
which is the point of the free-label design: the list describes what the
newspapers published rather than what somebody predicted they would.

Merging is the one destructive-looking operation, and it is not destructive.
`merge(src, dst)` rewrites the articles filed under `src` and leaves the row
in place pointing at `dst`, so the decision stays visible and reversible, and
an article that arrives under the old label later still lands in the right
place. A row is never deleted, because a deleted row means the same label
reappears tomorrow as a new entry and the merge silently un-does itself.

Failure is non-fatal. Every function degrades to leaving the label exactly as
the classifier produced it, which is a working system with an untidier list --
never a failed ingest.
"""

from __future__ import annotations

import datetime as _dt

from pymongo import ASCENDING, DESCENDING, MongoClient

from . import catdata, config


class CategoryStore:
    def __init__(self, uri: str | None = None, db: str | None = None):
        self.uri = config.mongo_uri(uri)
        self.name = config.mongo_db(db)
        self.client = MongoClient(self.uri, **config.mongo_kwargs())
        self.db = self.client[self.name]
        self.cats = self.db.categories
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.cats.create_index([("id", ASCENDING)], unique=True)
        self.cats.create_index([("n", DESCENDING)])
        self.cats.create_index([("merged_into", ASCENDING)])
        self.cats.create_index([("parent", ASCENDING)])

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    # -- writing -----------------------------------------------------------

    def observe(self, label: str, raw: str = "") -> str:
        """
        Record one sighting. Returns the entry it should actually be filed as.

        Called once per article at ingest, so it is a single upsert with an
        increment and no read. The return value matters: if this label has
        been merged away, the caller must store the target, otherwise the
        merge only applies to history and every new article re-splits the
        subject that was just joined.
        """
        lab = catdata.normalise(label) or catdata.UNKNOWN
        now = _dt.datetime.now()
        doc = self.cats.find_one_and_update(
            {"id": lab},
            {"$inc": {"n": 1},
             "$set": {"last_seen": now},
             # `raw` is a sample, not a list: keeping every spelling ever seen
             # would grow without bound for no decision it helps anybody make.
             # One example is enough to recognise what the model is writing.
             "$setOnInsert": {"label": catdata.display(lab), "sample_raw": raw,
                              "merged_into": None, "hidden": False,
                              # A heading is its own top level; anything else
                              # starts unplaced and is placed by a tree pass.
                              "parent": None if catdata.is_parent(lab)
                                        else catdata.seed_parent(lab) or None,
                              "first_seen": now}},
            upsert=True, return_document=True)
        return (doc or {}).get("merged_into") or lab

    # -- the tree ----------------------------------------------------------
    #
    # Two levels, and the asymmetry between them is the design. Parents come
    # from catdata.PARENTS and do not drift, because the top level is what a
    # reader scans and a picker whose headings change is unusable. Children are
    # whatever the classifier produced, because that is where the detail lives
    # and the merge tooling above can absorb the drift.
    #
    # A row's `parent` is null for a heading and set for everything under one.
    # Null on a CHILD means nobody has placed it yet -- which is a visible gap,
    # not an error, and epaper/classify.assign_parents fills it in one pass
    # over the distinct labels rather than one per article.

    def set_parent(self, cat_id: str, parent: str) -> dict:
        """Place one label under a heading. `parent=""` unplaces it."""
        cat_id = catdata.normalise(cat_id) or cat_id
        if not self.cats.find_one({"id": cat_id}):
            raise KeyError(cat_id)
        par = catdata.normalise(parent) if parent else ""
        if par:
            if not catdata.is_parent(par):
                raise ValueError(
                    "%r is not one of the headings: %s"
                    % (par, ", ".join(catdata.PARENTS)))
            if par == cat_id:
                raise ValueError("%r cannot be its own parent" % cat_id)
            # The heading has to exist as a row, or the picker has a parent it
            # cannot render and a filter nothing resolves against.
            self.create(par)
        self.cats.update_one({"id": cat_id},
                             {"$set": {"parent": par or None,
                                       "updated_at": _dt.datetime.now()}})
        return self.get(cat_id)

    def unplaced(self, *, min_count: int = 1) -> list:
        """
        Labels with no heading yet, most-used first.

        What `assign_parents` is run over, and what the admin screen shows as
        outstanding. Ordered by count because placing a label used four hundred
        times matters and one used twice does not.
        """
        # UNKNOWN is excluded: it is a sentinel meaning "the classifier could
        # not label this", not a subject. Placing it under a heading would put
        # a group of unlabelled articles in the picker as though they had
        # something in common, and asking a model where it belongs is a call
        # paid for an answer that can only be wrong.
        return list(self.cats.find(
            {"parent": None, "merged_into": None,
             "id": {"$nin": list(catdata.PARENTS) + [catdata.UNKNOWN]},
             "n": {"$gte": int(min_count)}},
            {"_id": 0}).sort("n", DESCENDING))

    def tree(self, *, include_hidden: bool = False) -> list:
        """
        The headings, each with its children. What the two-level picker renders.

        Every heading in catdata.PARENTS appears even at zero articles, so the
        top level is the same shape on every screen and on every day. A reader
        learning where "crime" sits should not have to relearn it because a
        quiet Tuesday had none.
        """
        rows = {r["id"]: r for r in self.listing(min_count=0,
                                                 include_hidden=include_hidden)}
        kids = {}
        for r in rows.values():
            if r.get("parent"):
                kids.setdefault(r["parent"], []).append(r)

        out = []
        for head in catdata.PARENTS:
            row = rows.get(head) or {"id": head, "label": catdata.display(head),
                                     "n": 0}
            children = sorted(kids.get(head, []), key=lambda r: -r["n"])
            out.append({
                "id": head, "label": row.get("label") or catdata.display(head),
                # The heading's own count is articles filed directly under it
                # with no child; `total` is those plus everything beneath.
                # The heading's own count already includes everything beneath
                # it, because an article carries its heading as well as its
                # own label. Adding the children again would double every
                # placed article.
                "n": row.get("n", 0),
                "total": row.get("n", 0),
                "children": [{"id": c["id"], "label": c["label"], "n": c["n"]}
                             for c in children],
            })
        return out

    def create(self, label: str, *, display: str = "") -> dict:
        """
        Add a topic nobody has used yet.

        Starts at zero articles rather than one: this is a heading somebody
        wants available, not a sighting, and counting it as a sighting would
        put a topic at the top of the review queue that no article carries.
        """
        lab = catdata.normalise(label)
        if not lab:
            raise ValueError("%r is not usable as a topic" % label)
        now = _dt.datetime.now()
        self.cats.update_one(
            {"id": lab},
            {"$setOnInsert": {"id": lab, "label": display.strip() or
                              catdata.display(lab), "n": 0,
                              "merged_into": None, "hidden": False,
                              "sample_raw": "", "created_by": "human",
                              "parent": None if catdata.is_parent(lab)
                                        else catdata.seed_parent(lab) or None,
                              "first_seen": now, "last_seen": now}},
            upsert=True)
        return self.get(lab)

    def observe_many(self, labels) -> dict:
        """Record a batch. Returns {label: effective label} for the caller."""
        return {lab: self.observe(lab) for lab in labels}

    # -- reading -----------------------------------------------------------

    def listing(self, *, min_count: int = 1, include_hidden: bool = False,
                include_merged: bool = False) -> list:
        """
        The topic list, most-used first. What the picker renders.

        Merged and hidden rows are excluded by default: both are entries
        somebody has explicitly decided a reader should not be offered.

        A category somebody created by hand is listed whatever its count. The
        `min_count` floor exists to keep one-off labels the classifier invented
        out of the picker, and a hand-made entry is the opposite of that -- it
        starts at n=0 by definition, because the reason to create one is to
        have it available BEFORE an article is filed under it. Applying the
        floor to those made the Categories tab accept a new category, report
        success, and then never show it.
        """
        floor = {"n": {"$gte": int(min_count)}}
        flt = {"$or": [floor, {"created_by": "human"}]} if min_count > 0 else floor
        if not include_merged:
            flt["merged_into"] = None
        if not include_hidden:
            flt["hidden"] = {"$ne": True}
        # Hand-made entries sort to the top of the zero-count tail rather than
        # being lost among labels that fell out of use.
        return list(self.cats.find(flt, {"_id": 0})
                    .sort([("n", DESCENDING), ("label", ASCENDING)]))

    def get(self, cat_id: str):
        return self.cats.find_one({"id": cat_id}, {"_id": 0})

    def resolve(self, cat_id: str) -> str:
        """
        Where a label should be filed today, following one merge hop.

        One hop, not a chain. A merged into B merged into C means the first
        merge should have been redirected when the second was made, and
        following the chain would hide that rather than surface it.
        """
        d = self.cats.find_one({"id": cat_id}, {"merged_into": 1})
        return (d or {}).get("merged_into") or cat_id

    def unmapped(self, limit: int = 40) -> list:
        """
        The review queue: entries seen often enough to matter, newest first.

        This is what somebody scans once a week to spot two rows that are one
        subject. Ordered by count rather than date because a label used twice
        is not worth a decision and a label used four hundred times is.
        """
        return list(self.cats.find(
            {"merged_into": None, "hidden": {"$ne": True}},
            {"_id": 0}).sort("n", DESCENDING).limit(limit))

    # -- editing -----------------------------------------------------------

    def merge(self, src: str, dst: str, *, articles=None) -> dict:
        """
        File everything under `src` as `dst` instead, now and in future.

        Rewrites the articles already filed, which is the half that makes a
        merge worth doing -- a merge that only affected new articles would
        leave the reader with the same split list they complained about.

        `articles` is the collection to rewrite; passing it explicitly keeps
        this module from having to know how Corpus is constructed.
        """
        src = catdata.normalise(src) or src
        dst = catdata.normalise(dst) or dst
        if not src or not dst:
            raise ValueError("merge needs two labels")
        if src == dst:
            raise ValueError("cannot merge %r into itself" % src)
        if not self.cats.find_one({"id": src}):
            raise KeyError(src)

        # The target must exist, so that merging into a label nobody has seen
        # yet creates it rather than pointing at nothing.
        now = _dt.datetime.now()
        self.cats.update_one(
            {"id": dst},
            {"$setOnInsert": {"id": dst, "label": catdata.display(dst), "n": 0,
                              "merged_into": None, "hidden": False,
                              "sample_raw": "", "first_seen": now,
                              "last_seen": now}},
            upsert=True)

        moved = 0
        if articles is not None:
            moved = articles.update_many({"category": src},
                                         {"$set": {"category": dst}}).modified_count

        src_doc = self.cats.find_one({"id": src}) or {}
        # A merge target with no heading inherits the source's. Otherwise
        # merging a placed label into an unplaced one quietly demotes it out
        # of the tree, and the articles vanish from the picker.
        dst_doc = self.cats.find_one({"id": dst}) or {}
        if src_doc.get("parent") and not dst_doc.get("parent")                 and not catdata.is_parent(dst):
            self.cats.update_one({"id": dst},
                                 {"$set": {"parent": src_doc["parent"]}})
        self.cats.update_one({"id": src},
                             {"$set": {"merged_into": dst, "updated_at": now}})
        self.cats.update_one({"id": dst},
                             {"$inc": {"n": int(src_doc.get("n") or 0)},
                              "$set": {"last_seen": now}})

        # Anything already pointing at src now points at dst, so no chain is
        # ever created and resolve() stays correct at one hop.
        self.cats.update_many({"merged_into": src},
                              {"$set": {"merged_into": dst, "updated_at": now}})

        return {"merged": src, "into": dst, "articles_moved": moved}

    def unmerge(self, src: str) -> dict:
        """
        Undo the pointer. Articles already rewritten stay where they are.

        Deliberately partial, and says so: the article rewrite is not tracked
        per document, so it cannot be reversed exactly. This restores the
        entry so new articles stop being redirected, which is the half that
        matters when a merge turns out to be wrong.
        """
        if not self.cats.find_one({"id": src}):
            raise KeyError(src)
        self.cats.update_one({"id": src},
                             {"$set": {"merged_into": None,
                                       "updated_at": _dt.datetime.now()}})
        return {"restored": src,
                "note": "articles already rewritten were not moved back"}

    def rename(self, cat_id: str, label: str) -> dict:
        """Change how an entry is shown. The stored id never changes."""
        if not self.cats.find_one({"id": cat_id}):
            raise KeyError(cat_id)
        self.cats.update_one({"id": cat_id},
                             {"$set": {"label": label.strip(),
                                       "updated_at": _dt.datetime.now()}})
        return self.get(cat_id)

    def set_hidden(self, cat_id: str, on: bool) -> dict:
        """
        Keep an entry out of the picker without merging or deleting it.

        For labels that are real but useless to a reader -- "other", "general",
        anything the model produced that names no subject.
        """
        if not self.cats.find_one({"id": cat_id}):
            raise KeyError(cat_id)
        self.cats.update_one({"id": cat_id},
                             {"$set": {"hidden": bool(on),
                                       "updated_at": _dt.datetime.now()}})
        return self.get(cat_id)

    def rebuild(self, articles) -> dict:
        """
        Recount from the articles themselves.

        The stored counts are incremented as articles arrive, so they drift
        whenever articles are deleted, re-ingested or relabelled. This is the
        repair: the articles are the truth and this makes the list agree with
        them. Rows that no article carries any more are kept at zero rather
        than removed, so a merge decision made about them survives.
        """
        # Counted at BOTH levels. A heading's count is the articles filed under
        # it; a child's is the articles carrying that child. Counting only
        # `category` -- which is what this did before the tree existed -- left
        # every child at zero the moment it was placed, because its articles
        # now carry the heading in that field and the label in the other one.
        counts = {x["_id"]: x["n"] for x in articles.aggregate([
            {"$match": {"category": {"$nin": ["", None]}}},
            {"$group": {"_id": "$category", "n": {"$sum": 1}}}])}
        for x in articles.aggregate([
                {"$match": {"subcategory": {"$nin": ["", None]}}},
                {"$group": {"_id": "$subcategory", "n": {"$sum": 1}}}]):
            counts[x["_id"]] = counts.get(x["_id"], 0) + x["n"]
        now = _dt.datetime.now()
        for lab, n in counts.items():
            self.cats.update_one(
                {"id": lab},
                {"$set": {"n": n, "last_seen": now},
                 "$setOnInsert": {"label": catdata.display(lab), "sample_raw": "",
                                  "merged_into": None, "hidden": False,
                                  "first_seen": now}},
                upsert=True)
        self.cats.update_many({"id": {"$nin": list(counts)}}, {"$set": {"n": 0}})
        return {"topics": len(counts), "articles": sum(counts.values())}


# ------------------------------------------------------------------ helper

_store = None
_error = ""


def store(**kw):
    """Process-wide CategoryStore, or None with the reason in `last_error()`."""
    global _store, _error
    if _store is None:
        try:
            s = CategoryStore(**kw)
            if not s.ping():
                raise RuntimeError("mongodb not reachable")
            _store, _error = s, ""
        except Exception as ex:
            _error = str(ex)[:200]
            return None
    return _store


def last_error() -> str:
    return _error


def file_child(label: str, parent: str = "") -> str:
    """
    Record one sighting of a subcategory, under its heading. Returns the entry.

    The registry has to see the second level as well as the first, or
    `unplaced()` is always empty and `assign_parents` has nothing to place --
    the counts that decide which labels are worth a decision live here.

    The parent is set only when the row does not already have one. A placement
    made by hand, or by an earlier pass, is a decision; letting every ingest
    overwrite it would mean a correction lasting until the next article
    carrying that label arrived.
    """
    lab = catdata.normalise(label)
    if not lab:
        return ""
    s = store()
    if not s:
        return lab
    try:
        filed = s.observe(lab, lab)
        if parent and catdata.is_parent(parent):
            row = s.get(filed) or {}
            if not row.get("parent"):
                s.set_parent(filed, parent)
        return filed
    except Exception:
        return lab


def file_as(label: str, raw: str = "") -> str:
    """
    The entry one article should be filed under, recording the sighting.

    The call ingest makes. Degrades to plain normalisation when MongoDB is
    unreachable, so a database that is down costs the frequency counts and the
    merge redirects -- not the label, and not the article.
    """
    s = store()
    if not s:
        return catdata.normalise(label) or catdata.UNKNOWN
    try:
        return s.observe(label, raw)
    except Exception:
        return catdata.normalise(label) or catdata.UNKNOWN
