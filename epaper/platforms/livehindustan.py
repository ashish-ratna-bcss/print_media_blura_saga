"""
Hindustan (Hindi) e-paper client -- epaper.livehindustan.com.

The third platform, and the reason it exists is coverage: the beat vocabulary
carries all 36 state forces while the newspaper catalogue reached about a
third of them. Hindustan is a Hindi daily across Uttar Pradesh, Bihar and
Delhi, which is a large part of what was missing.

It was picked after testing the obvious candidates, and the test that mattered
was resolution, not whether a parser could be written:

    Hindustan       2438 x 4000   full edition, no login
    Dainik Jagran    590 x  962   thumbnail only; no larger variant exists
    Dainik Bhaskar   497 x  783   page 1 only, rest 403, reader needs login
    Amar Ujala        --          403 even on page 1, hotlink-protected

Only the first is usable. At 590px a newspaper body line is about three pixels
tall -- YOLO would find blocks and Tesseract would read nothing, so a parser
for those would have been a parser that returns unreadable pages.

How it works
------------
A Next.js site that server-renders its entire state into __NEXT_DATA__, so
there is no API to authenticate against and no tile pyramid to stitch. One GET
per edition yields the city list, the issue date, and every page with a direct
image URL. That makes this the simplest of the three clients by some way --
compare readwhere.py, which has to assemble tiles from a pyramid, and note
that Hindustan's single image is nonetheless the higher resolution of the two.

    /edition/{city}          -> __NEXT_DATA__ with cityOptions + edition.pages
    page["viewerSrc"]        -> .../{...}_hr.webp   2438 x 4000
    page["thumbnailSrc"]     -> .../{...}_tn.webp     122 x  200

`_hr` is what to fetch. The `_tn` variant is a contact-sheet thumbnail and is
useless for extraction.

Caveats worth knowing
---------------------
  - Not every city publishes every day, and a city that has not published yet
    returns `totalPages: 0` rather than an error. `pages()` returns an empty
    list; that is not a failure.
  - The site sets PAYWALL_ENABLED and each edition carries a `paywallPage`.
    It is None on every edition observed, but `paywalled_from()` surfaces it
    rather than assuming it will stay that way.
"""

from __future__ import annotations

import json
import re

from ..probe import fetch

_NEXT = re.compile(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', re.S)


class LiveHindustan:
    def __init__(self, base_url: str = "epaper.livehindustan.com"):
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        self.base = base_url.rstrip("/")
        self._cache: dict = {}

    # -- fetching ----------------------------------------------------------

    def _props(self, path: str) -> dict:
        """
        The server-rendered pageProps for a route, or {}.

        Cached per path: the city list and the page list come out of the same
        document, and callers routinely want both.
        """
        if path in self._cache:
            return self._cache[path]
        _, _, body = fetch(self.base + path, timeout=45)
        m = _NEXT.search(body or "")
        if not m:
            return {}
        try:
            props = json.loads(m.group(1))["props"]["pageProps"]
        except (ValueError, KeyError):
            return {}
        self._cache[path] = props
        return props

    # -- discovery ---------------------------------------------------------

    def cities(self) -> list:
        """
        Editions offered, as [{id, name, name_hi, region}].

        `id` is the URL slug and the only handle the site accepts. Names come
        back in Devanagari; `region` is the state, also in Devanagari, which
        is what lets these be matched against a beat.
        """
        props = self._props("/edition/delhi") or self._props("/")
        out = []
        for c in props.get("cityOptions") or []:
            slug = c.get("slug")
            if not slug:
                continue
            out.append({"id": slug, "name": c.get("name") or slug,
                        "name_hi": c.get("name") or "",
                        "region": c.get("region") or ""})
        return out

    def edition(self, city: str, date: str | None = None) -> dict:
        """
        One city's issue. `date` as YYYY-MM-DD; omitted means today's.

        A city that has not published yet comes back with totalPages 0 and an
        empty page list rather than an error, so callers must check rather
        than assume.
        """
        path = "/edition/%s" % city
        if date:
            path += "?date=%s" % date
        e = (self._props(path) or {}).get("edition") or {}
        return {
            "city": (e.get("city") or {}).get("slug") or city,
            "name": (e.get("city") or {}).get("name") or city,
            "region": (e.get("city") or {}).get("region") or "",
            "edition_id": e.get("editionId") or "",
            "date": e.get("issueDateIso") or "",
            "label": e.get("issueLabel") or "",
            "total_pages": int(e.get("totalPages") or 0),
            "paywall_from": e.get("paywallPage"),
            "pages": [self._page(p) for p in (e.get("pages") or [])],
        }

    @staticmethod
    def _page(p: dict) -> dict:
        return {
            "page_no": int(p.get("pageNumber") or 0),
            "url": p.get("viewerSrc") or "",
            "thumb": p.get("thumbnailSrc") or "",
            # The dimensions the viewer reports, NOT the file's. The viewer
            # scales to 1524x2500; the _hr.webp behind it is 2438x4000.
            "viewer_width": int(p.get("viewerWidth") or 0),
            "viewer_height": int(p.get("viewerHeight") or 0),
            "section": p.get("section") or "",
            "page_id": p.get("pageId") or "",
            "supplement": p.get("supplement") or "",
        }

    def pages(self, city: str, date: str | None = None) -> list:
        """Just the pages, in order. Empty when the issue is not out yet."""
        return sorted(self.edition(city, date)["pages"],
                      key=lambda p: p["page_no"])

    def page_url(self, city: str, page_no: int, date: str | None = None) -> str:
        """Direct image URL for one page, or "" if that page does not exist."""
        for p in self.pages(city, date):
            if p["page_no"] == int(page_no):
                return p["url"]
        return ""

    def paywalled_from(self, city: str, date: str | None = None):
        """
        First page behind the paywall, or None if the whole issue is open.

        None on every edition observed, but the site ships PAYWALL_ENABLED and
        the field is per-issue, so this reports what the server said instead
        of encoding today's answer as an assumption.
        """
        return self.edition(city, date).get("paywall_from")
