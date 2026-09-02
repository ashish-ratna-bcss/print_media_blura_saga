"""
Readwhere e-paper platform client.

Powers Financial Express, The Statesman, The Tribune, Jansatta and Dainik
Tribune among the Delhi titles. Everything below works without login.

What Readwhere gives you:
  - titles (editions) per publisher
  - a date -> issue-id index, so you can crawl backwards for months
  - page images as tile pyramids, up to ~2400px wide

What it does NOT give you, verified against The Tribune:
  - publisher article segmentation. The `clips` in the viewer are USER-created
    clippings and come back empty. There is no article zone data and no text
    layer. Pages from here must go through the CV/VLM segmentation stage.

Endpoint shapes (reverse engineered from viewer-v5.js):
  {base}viewer/publishdates/{titleId}/{fromTs}/{toTs}/json
  {base}pagemeta/{get|getsample}/{issueId}/{p1}-{p2}
  {base}viewer/pageclips/{issueId}/{pageKey}
  https://cache.epapr.in/{issueId}/{pageKey}/{W}x{H}-{tw}x{th}/{col}x{row}.jpg
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import urllib.parse

from ..probe import fetch


class Readwhere:
    def __init__(self, base_url: str):
        if not base_url.startswith("http"):
            base_url = "https://" + base_url
        self.base = base_url.rstrip("/") + "/"

    # -- discovery -------------------------------------------------------

    def titles(self) -> list:
        """
        Editions offered by this publisher, as {id, name}.

        Readwhere themes differ in where they put the edition name, so all
        three observed shapes are handled:
          /t/690              name in the anchor text    (Tribune)
          /t/227/Delhi        name as a path segment     (Financial Express)
          /t/22557/?s=Kolkata name in a query param      (Jansatta, Statesman)
        """
        _, _, html = fetch(self.base)
        host = re.escape(urllib.parse.urlsplit(self.base).netloc)
        names, order = {}, []

        def record(tid, name):
            name = re.sub(r"\s+", " ", (name or "")).strip(" >-")
            if tid not in names:
                order.append(tid)
                names[tid] = ""
            # Prefer a real name over a placeholder picked up earlier.
            if name and name.lower() != "latest" and not names[tid]:
                names[tid] = name

        for m in re.finditer(r'href="https?://%s/t/(\d+)([^"]*)"' % host, html):
            tid, rest = m.group(1), m.group(2)
            q = re.search(r"[?&]s=([^&\"]+)", rest)
            if q:
                name = urllib.parse.unquote_plus(q.group(1))
            else:
                segs = [x for x in rest.split("/") if x and x != "latest"]
                name = urllib.parse.unquote_plus(
                    segs[0].split("?")[0]).replace("-", " ") if segs else ""
            record(tid, name)

        for m in re.finditer(
                r'href="https?://%s/t/(\d+)[^"]*"(.{0,300}?)</a>' % host, html, re.S):
            record(m.group(1), re.sub(r"<[^>]+>", " ", m.group(2)))

        return [{"id": t, "name": names[t] or ("title %s" % t)} for t in order]

    def publish_dates(self, title_id, days_back: int = 45) -> list:
        """Issues published for a title. Returns [{published, id}] oldest first."""
        now = _dt.datetime.now()
        frm = int((now - _dt.timedelta(days=days_back)).timestamp())
        to = int((now + _dt.timedelta(days=2)).timestamp())
        url = "%sviewer/publishdates/%s/%d/%d/json" % (self.base, title_id, frm, to)
        _, _, body = fetch(url)
        try:
            return json.loads(body)
        except Exception:
            return []

    def issue_for_date(self, title_id, date: str):
        """date as YYYY-MM-DD -> issue id, or None if that day wasn't published."""
        if not date:
            raise ValueError("issue_for_date needs a date as YYYY-MM-DD")
        for row in self.publish_dates(title_id):
            # `or ""` rather than a get() default: the default only applies
            # when the key is ABSENT, and a row carrying an explicit null
            # published date would still reach startswith as None and raise.
            if (row.get("published") or "").startswith(date):
                return row["id"]
        return None

    # -- pages -----------------------------------------------------------

    def page_meta(self, issue_id, first: int = 1, last: int = None) -> dict:
        """Tile pyramids for a page range. Keyed by page number as a string."""
        last = last or first
        url = "%spagemeta/get/%s/%d-%d" % (self.base, issue_id, first, last)
        _, _, body = fetch(url)
        try:
            data = json.loads(body)
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def all_page_meta(self, issue_id, num_pages: int) -> dict:
        """
        pagemeta only returns a couple of pages per call, so walk the range.

        May return far fewer pages than `num_pages`, and that is not an error:
        on a gated title the server answers every range with page 1 instead of
        refusing, so the walk collapses to a single entry. Callers must treat
        the returned keys as the pages actually available and never assume
        `1..num_pages`. Compare `len(out)` against `num_pages` to detect it.
        """
        out = {}
        for start in range(1, num_pages + 1, 2):
            out.update(self.page_meta(issue_id, start, min(start + 1, num_pages)))
        return out

    @staticmethod
    def best_level(meta_for_page: dict) -> dict:
        """Highest-resolution level available for a page."""
        levels = meta_for_page.get("levels", {})
        real = {k: v for k, v in levels.items() if k not in ("header", "thumbs")}
        if not real:
            return {}
        return max(real.values(), key=lambda v: v.get("width", 0))

    @staticmethod
    def tiles(level: dict) -> list:
        """
        Tiles for a level, as {x, y, w, h, base: url, text: url}.

        Readwhere renders a page as TWO layers at the same grid position:
          .jpg  opaque background -- photographs and display advertising
          .png  transparent overlay -- all of the editorial TEXT

        Compositing both is mandatory. Take the JPEG alone and you get a page
        with pictures and ads but no words, which looks like a paywall but is
        really just a dropped layer.
        """
        grid = {}
        for c in level.get("chunks", []):
            url = c.get("url", "").replace("http://", "https://")
            k = (int(c["tx"]), int(c["ty"]))
            slot = grid.setdefault(k, {
                "x": k[0], "y": k[1],
                "w": int(c["width"]), "h": int(c["height"]),
                "base": None, "text": None,
            })
            if url.endswith(".png"):
                slot["text"] = url
            elif url.endswith(".jpg"):
                slot["base"] = url
        return [grid[k] for k in sorted(grid)]

    def issue_config(self, issue_id) -> dict:
        """Scrape DEConfig off the viewer page for numPages, title, publish time."""
        _, _, html = fetch("%sr/%s" % (self.base, issue_id))
        cfg = {}
        for key, cast in (("numPages", int), ("titleId", str), ("titleName", str),
                          ("published", str), ("publisherId", str)):
            m = re.search(r"'%s':\s*\"?([^,\"']+)\"?" % key, html)
            if m:
                try:
                    cfg[key] = cast(m.group(1).strip())
                except Exception:
                    cfg[key] = m.group(1).strip()
        return cfg
