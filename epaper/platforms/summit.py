"""
Summit India e-paper platform client.

This is the good case. Where a publisher has tagged its stories, this platform
hands back the article text the DTP system produced -- no OCR, no layout
analysis, no reading-order reconstruction. Verified on epaper.ntnews.com
(Namasthe Telangana) and epaper.sakshi.com, which expose identical routes.

Coverage is uneven and must be checked per edition: on Namasthe Telangana the
two main editions are fully tagged (120 articles across 12 pages) while every
district edition returns an empty story list and needs the CV pipeline.

Dates are DD/MM/YYYY. Passing YYYY-MM-DD silently returns an empty list.
"""

from __future__ import annotations

import json
import re

from ..probe import fetch


class Summit:
    def __init__(self, base_url: str):
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        self.base = base_url.rstrip("/") + "/Home/"

    def _json(self, path: str, params: dict = None, post: dict = None):
        url = self.base + path
        if params:
            import urllib.parse
            url += "?" + urllib.parse.urlencode(params)
        if post is not None:
            import urllib.request
            from ..probe import UA
            req = urllib.request.Request(
                url, data=json.dumps(post).encode(),
                headers={"User-Agent": UA,
                         "Content-Type": "application/json; charset=utf-8",
                         "Accept-Encoding": "identity"})
            try:
                import urllib.error
                with urllib.request.urlopen(req, timeout=40) as r:
                    return json.loads(r.read().decode("utf-8", "ignore"))
            except Exception:
                return None
        _, _, body = fetch(url, timeout=40)
        try:
            return json.loads(body)
        except Exception:
            return None

    # -- structure -------------------------------------------------------

    def editions(self) -> list:
        """Flattened edition list: [{group, name, id}]."""
        data = self._json("GetEditionsHierarchy") or []
        out = []
        for grp in data:
            for e in grp.get("editionlocation") or []:
                out.append({"group": grp.get("org_location", ""),
                            "name": e.get("Editionlocation", "").strip(),
                            "id": e.get("EditionId")})
        return out

    def pages(self, edition_id, date_ddmmyyyy: str) -> list:
        """Pages for an edition on a date, with direct CDN image URLs."""
        return self._json("GetAllpagespost", post={
            "editionid": str(edition_id), "editiondate": date_ddmmyyyy}) or []

    # -- articles --------------------------------------------------------

    def stories_on_page(self, page_id) -> list:
        """One row per article. Empty list means this page was never tagged."""
        r = self._json("getStoriesOnPage", params={"pageid": page_id})
        return r if isinstance(r, list) else []

    def story(self, story_id) -> dict:
        """Full article: headline, body, byline, photos, captions, continuations."""
        r = self._json("getstorydetail", params={"Storyid": story_id})
        return r if isinstance(r, dict) else {}

    # -- normalisation ---------------------------------------------------

    @staticmethod
    def clean_body(story: dict) -> str:
        """Strip the HTML wrapper off the story body."""
        sc = (story.get("StoryContent") or [{}])[0]
        html = sc.get("Body") or ""
        text = re.sub(r"</p>\s*<p>", "\n", html)
        text = re.sub(r"<[^>]+>", "", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    @staticmethod
    def clip_url(story: dict) -> str:
        """Publisher's own article crop. Path uses Windows separators."""
        p = story.get("filepathstorypic") or ""
        return p.replace("\\", "/")

    @staticmethod
    def raw_headlines(story: dict) -> list:
        """
        Headline strings as stored. NOTE: on Telugu titles these are legacy
        non-Unicode font bytes (e.g. 'b˜\xcd\xab$T\xa9!' renders as
        'ఫ్యామిలీ!'). The body is proper Unicode; only headlines need either a
        font mapping table or OCR of the headline strip off the clip image.
        """
        sc = (story.get("StoryContent") or [{}])[0]
        return sc.get("Headlines") or []

    def article(self, story_row: dict) -> dict:
        """Merge list row + detail into one normalised record."""
        detail = self.story(story_row["storyid"])
        return {
            "story_id": story_row["storyid"],
            "page_id": story_row.get("Pageid"),
            "org_id": story_row.get("OrgId"),
            "headlines_raw": self.raw_headlines(detail),
            "body": self.clean_body(detail),
            "summary": story_row.get("Summary", ""),
            "byline": detail.get("Byline", ""),
            "dateline": detail.get("dateLine", ""),
            "kicker": detail.get("HeadKicker", ""),
            "clip_image": self.clip_url(detail),
            "photos": [{"id": p.get("PictureId"), "caption": p.get("caption", ""),
                        "url": p.get("fullpathlinkpic", "")}
                       for p in (detail.get("LinkPicture") or [])],
            "continued_in": detail.get("LinkedStoryIds") or [],
            "edition_name": detail.get("EditionName", ""),
            "page_number": detail.get("PageNumber", ""),
            "date": detail.get("Eddate", ""),
            "text_source": "publisher",   # not OCR -- exact DTP text
        }
