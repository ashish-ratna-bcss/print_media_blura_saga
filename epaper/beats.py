"""
Beats in MongoDB: seed them, edit them at runtime, load them back.

epaper/beatdata.py holds the vocabulary that ships with the code. This module
is the layer that makes it editable without a deploy -- the "if anything is
missed we will add it from the backend" half. Two collections:

    beat_packs   a vocabulary shared by many beats. `policing` is the only one
                 today: ~290 terms that are identical in every state, because
                 a constable is a constable in Kohima and in Kochi. Adding a
                 term here adds it to all 36 forces at once.

    beats        one force. Its name, its aliases, its geography, and any
                 terms peculiar to it. A state beat is a pack reference plus a
                 list of place names.

`region_out` -- the geography that marks a story as somebody else's -- is NOT
stored. It is computed at load time as the union of every other beat's places.
Storing it would mean that adding one district to Punjab requires rewriting 35
other documents, and the first missed rewrite leaves the gate silently wrong.
A beat may still pin an explicit list if it needs one.

Failure is non-fatal by design. A Mongo that is down leaves keywords.BEATS
holding the 36 built-in beats, which is a complete working vocabulary. Nothing
here is on the path of a search that does not need it.
"""

from __future__ import annotations

import datetime as _dt

from pymongo import ASCENDING, MongoClient

from . import beatdata, config, keywords

TIERS = {"core": beatdata.CORE, "related": beatdata.RELATED,
         "context": beatdata.CONTEXT}


def tier_of(weight: float) -> str:
    for name, w in TIERS.items():
        if abs(w - float(weight)) < 1e-6:
            return name
    return "custom"


# ------------------------------------------------------------- conversion

def _groups_to_docs(groups) -> list:
    """[(1.0, [...])] -> [{"w": 1.0, "tier": "core", "terms": [...]}]"""
    return [{"w": float(w), "tier": tier_of(w), "terms": list(terms)}
            for w, terms in (groups or [])]


def _docs_to_groups(docs) -> list:
    """The inverse. Tolerates a missing weight by falling back to the tier."""
    out = []
    for d in docs or []:
        w = d.get("w")
        if w is None:
            w = TIERS.get(d.get("tier") or "", beatdata.CONTEXT)
        out.append((float(w), list(d.get("terms") or [])))
    return out


def to_doc(beat: dict) -> dict:
    """A keywords-shaped beat to a MongoDB document."""
    return {
        "id": beat["id"],
        "label": beat.get("label") or beat["id"],
        "kind": beat.get("kind", "custom"),
        "state": beat.get("state", ""),
        "state_hi": beat.get("state_hi", ""),
        "state_code": beat.get("state_code", ""),
        "region_kind": beat.get("region_kind", ""),
        "topic": beat.get("topic", ""),
        "aliases": list(beat.get("aliases") or []),
        "pack": beat.get("pack") or "",
        # Only what is peculiar to this beat. The pack's terms are not copied
        # in: that is the whole point of the pack, and duplicating them here
        # would mean an edit to the pack silently failing to reach this beat.
        "terms": _groups_to_docs(beat.get("own_terms")),
        "acronyms": _groups_to_docs(beat.get("own_acronyms")),
        "region": list(beat.get("region") or []),
        # null means "compute from every other beat" -- see the module note.
        "region_out": (list(beat["region_out"])
                       if isinstance(beat.get("region_out"), list)
                       and beat.get("region_out_pinned") else None),
        "enabled": bool(beat.get("enabled", True)),
        "builtin": bool(beat.get("builtin")),
    }


class BeatStore:
    def __init__(self, uri: str | None = None, db: str | None = None):
        self.uri = config.mongo_uri(uri)
        self.name = config.mongo_db(db)
        self.client = MongoClient(self.uri, **config.mongo_kwargs())
        self.db = self.client[self.name]
        self.beats = self.db.beats
        self.packs = self.db.beat_packs
        self._ensure_indexes()

    def _ensure_indexes(self):
        self.beats.create_index([("id", ASCENDING)], unique=True)
        self.beats.create_index([("kind", ASCENDING)])
        self.beats.create_index([("enabled", ASCENDING)])
        self.packs.create_index([("id", ASCENDING)], unique=True)

    def ping(self) -> bool:
        try:
            self.client.admin.command("ping")
            return True
        except Exception:
            return False

    # -- seeding -----------------------------------------------------------

    def seed(self, *, force: bool = False) -> dict:
        """
        Write the built-in vocabulary into MongoDB.

        Idempotent, and by default it will not overwrite a beat that already
        exists -- someone may have edited it, and a redeploy quietly reverting
        their additions is worse than a stale seed. `force=True` restores the
        shipped definitions.
        """
        now = _dt.datetime.now()
        counts = {"packs": 0, "added": 0, "kept": 0, "restored": 0}

        pack = {"id": "policing",
                "label": "Policing (shared across every force)",
                "terms": _groups_to_docs(beatdata.POLICING_TERMS),
                "acronyms": _groups_to_docs(beatdata.POLICING_ACRONYMS),
                "aliases": list(beatdata.POLICING_ALIASES),
                "updated_at": now}
        if force or not self.packs.find_one({"id": "policing"}):
            self.packs.update_one({"id": "policing"},
                                  {"$set": pack,
                                   "$setOnInsert": {"created_at": now}},
                                  upsert=True)
            counts["packs"] = 1

        for beat in beatdata.build_beats():
            doc = to_doc(dict(beat, own_terms=[], own_acronyms=[]))
            existing = self.beats.find_one({"id": doc["id"]})
            if existing and not force:
                counts["kept"] += 1
                continue
            doc["updated_at"] = now
            self.beats.update_one({"id": doc["id"]},
                                  {"$set": doc,
                                   "$setOnInsert": {"created_at": now}},
                                  upsert=True)
            counts["restored" if existing else "added"] += 1
        return counts

    # -- reading -----------------------------------------------------------

    def raw(self, *, enabled_only: bool = False) -> list:
        flt = {"enabled": True} if enabled_only else {}
        return list(self.beats.find(flt, {"_id": 0}))

    def get(self, beat_id: str):
        return self.beats.find_one({"id": beat_id}, {"_id": 0})

    def pack(self, pack_id: str):
        return self.packs.find_one({"id": pack_id}, {"_id": 0})

    def resolved(self, *, enabled_only: bool = False) -> list:
        """
        Stored documents assembled into the shape keywords.py expects.

        Where the work happens: a beat's terms are its pack's terms, then its
        own geography as a CONTEXT group, then anything peculiar to it. That
        ordering matters -- keywords._split_terms recovers the pack by removing
        the geography, so the geography has to be separable.
        """
        packs = {p["id"]: p for p in self.packs.find({}, {"_id": 0})}
        docs = self.raw(enabled_only=enabled_only)
        by_id = {d["id"]: d for d in docs}

        # region_out is derived, so it is computed over every beat that exists,
        # not just the enabled ones. Disabling Punjab must not make Ludhiana
        # stop counting as elsewhere for Delhi.
        every = {d["id"]: set(d.get("region") or [])
                 for d in self.raw(enabled_only=False)}

        out = []
        for d in docs:
            pk = packs.get(d.get("pack") or "")
            terms = _docs_to_groups(pk.get("terms")) if pk else []
            acronyms = _docs_to_groups(pk.get("acronyms")) if pk else []
            region = list(d.get("region") or [])
            if region:
                terms = terms + [(beatdata.CONTEXT, region)]
            terms = terms + _docs_to_groups(d.get("terms"))
            acronyms = acronyms + _docs_to_groups(d.get("acronyms"))

            aliases = list(d.get("aliases") or [])
            if pk and not aliases:
                aliases = list(pk.get("aliases") or [])

            pinned = d.get("region_out")
            if isinstance(pinned, list) and pinned:
                elsewhere = list(pinned)
            else:
                mine = every.get(d["id"], set())
                elsewhere = sorted({p for bid, ps in every.items()
                                    if bid != d["id"] for p in ps} - mine)

            out.append({
                "id": d["id"], "label": d.get("label") or d["id"],
                "kind": d.get("kind", "custom"),
                "state": d.get("state", ""), "state_hi": d.get("state_hi", ""),
                "state_code": d.get("state_code", ""),
                "region_kind": d.get("region_kind", ""),
                "topic": d.get("topic", ""),
                "aliases": aliases,
                "acronyms": acronyms,
                "terms": terms,
                "region": region,
                "region_out": elsewhere,
                "pack": d.get("pack") or "",
                "enabled": d.get("enabled", True),
                "builtin": bool(d.get("builtin")),
            })
        by_id.clear()
        return out

    def load_into_keywords(self, *, replace: bool = True) -> int:
        """
        Install what MongoDB holds. Returns how many beats are now active.

        `replace=True` because the database is the authority once seeded: a
        beat deleted there should disappear, and a merge would resurrect it
        from the built-ins on every restart.
        """
        beats = self.resolved()
        if not beats:
            return len(keywords.BEATS)          # empty DB: keep the built-ins
        return keywords.load(beats, replace=replace)

    # -- editing -----------------------------------------------------------

    def upsert(self, beat: dict) -> dict:
        """Create or replace one beat from an API payload."""
        if not beat.get("id"):
            raise ValueError("a beat needs an id")
        doc = {k: v for k, v in beat.items() if k != "_id"}
        doc.setdefault("enabled", True)
        doc.setdefault("kind", "custom")
        for key in ("terms", "acronyms"):
            if doc.get(key) and isinstance(doc[key][0], (list, tuple)):
                doc[key] = _groups_to_docs(doc[key])
        doc["updated_at"] = _dt.datetime.now()
        self.beats.update_one({"id": doc["id"]},
                              {"$set": doc,
                               "$setOnInsert": {"created_at": doc["updated_at"]}},
                              upsert=True)
        return self.get(doc["id"])

    def add_terms(self, beat_id: str, tier: str, terms: list) -> dict:
        """
        Add terms to ONE beat. The narrow case: something specific to a state.

        Adding to a beat that uses a pack makes its vocabulary diverge from the
        other 35, which is intended here and handled correctly -- keywords
        fingerprints the vocabulary rather than the pack name, so this beat
        gets its own compile and the rest keep sharing one.
        """
        return self._append(self.beats, {"id": beat_id}, "terms", tier, terms)

    def add_pack_terms(self, pack_id: str, tier: str, terms: list) -> dict:
        """Add terms to every beat using this pack. The usual case."""
        return self._append(self.packs, {"id": pack_id}, "terms", tier, terms)

    def add_places(self, beat_id: str, places: list) -> dict:
        """
        Extend a beat's geography.

        One write, no fan-out: because `region_out` is derived at load time,
        adding a district to Punjab automatically makes it count as elsewhere
        for the other 35 beats the next time they load.
        """
        doc = self.get(beat_id)
        if not doc:
            raise KeyError(beat_id)
        merged = list(doc.get("region") or [])
        merged += [p for p in places if p and p not in merged]
        self.beats.update_one({"id": beat_id},
                              {"$set": {"region": merged,
                                        "updated_at": _dt.datetime.now()}})
        return self.get(beat_id)

    @staticmethod
    def _append(col, key, field, tier, terms) -> dict:
        if tier not in TIERS:
            raise ValueError("tier must be one of %s" % ", ".join(TIERS))
        doc = col.find_one(key)
        if not doc:
            raise KeyError(key.get("id"))
        groups = list(doc.get(field) or [])
        target = next((g for g in groups if g.get("tier") == tier), None)
        if target is None:
            target = {"w": TIERS[tier], "tier": tier, "terms": []}
            groups.append(target)
        have = set(target["terms"])
        target["terms"] += [t for t in terms if t and t not in have]
        col.update_one(key, {"$set": {field: groups,
                                      "updated_at": _dt.datetime.now()}})
        return col.find_one(key, {"_id": 0})

    def set_enabled(self, beat_id: str, on: bool) -> dict:
        self.beats.update_one({"id": beat_id},
                              {"$set": {"enabled": bool(on),
                                        "updated_at": _dt.datetime.now()}})
        return self.get(beat_id)

    def delete(self, beat_id: str) -> dict:
        """
        Remove a custom beat. A built-in is disabled instead of deleted.

        Deleting one would make it reappear at the next seed, which reads as
        the delete having silently failed. Disabling is the honest equivalent
        and survives a reseed.
        """
        doc = self.get(beat_id)
        if not doc:
            raise KeyError(beat_id)
        if doc.get("builtin"):
            return {"disabled": self.set_enabled(beat_id, False)["id"],
                    "deleted": None}
        self.beats.delete_one({"id": beat_id})
        return {"disabled": None, "deleted": beat_id}


# ------------------------------------------------------------------ helper

_store = None
_error = ""


def store(**kw):
    """Process-wide BeatStore, or None with the reason in `last_error()`."""
    global _store, _error
    if _store is None:
        try:
            s = BeatStore(**kw)
            if not s.ping():
                raise RuntimeError("mongodb not reachable")
            _store, _error = s, ""
        except Exception as ex:
            _error = str(ex)[:200]
            return None
    return _store


def last_error() -> str:
    return _error


def bootstrap(*, seed_if_empty: bool = True) -> dict:
    """
    Called once at web app startup.

    Seeds an empty database with the built-in vocabulary, then installs
    whatever is stored. Never raises: an unreachable Mongo leaves the built-in
    36 beats in place, which is a complete working vocabulary, and the app
    starts.
    """
    s = store()
    if not s:
        return {"loaded": len(keywords.BEATS), "source": "built-in",
                "error": _error}
    try:
        seeded = {}
        if seed_if_empty and s.beats.count_documents({}) == 0:
            seeded = s.seed()
        n = s.load_into_keywords()
        return {"loaded": n, "source": "mongodb", "seeded": seeded,
                "database": s.name}
    except Exception as ex:
        return {"loaded": len(keywords.BEATS), "source": "built-in",
                "error": str(ex)[:200]}
