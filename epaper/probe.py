"""
Platform discovery for e-paper sites.

The core insight of this project: most e-paper viewers have a "tap an article to
read it" feature, and that feature is always backed by article-level data the
publisher already produced. Finding that data source is worth far more than any
amount of OCR. This module fingerprints an e-paper host, identifies the vendor
platform behind it, and probes for the article API.

Output is a ProbeResult per host, written to JSON so the extractor can be
pointed at whatever was found.
"""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Vendor signatures, checked against page HTML and linked JS. Ordered by how
# specific the marker is -- a SAM_base.js hit is conclusive, "epaper" is not.
SIGNATURES = [
    ("summit", [r"SAM_base\.js", r"summitindia\.com", r"getStoriesOnPage", r"GetAllpagespost"]),
    ("readwhere", [r"readwhere\.com", r"rwcdn", r"readwhere\.in", r"/viewer/\d+"]),
    ("olive", [r"olivesoftware", r"Olive\.ODN", r"/olive/odn/", r"ActiveMagazine"]),
    ("pressreader", [r"pressreader\.com", r"NewspaperDirect"]),
    ("magzter", [r"magzter\.com"]),
    ("erelego", [r"erelego\.com"]),
    ("newsprint", [r"newsprint\.in", r"paperboy"]),
    ("hindu_custom", [r"epaper\.thehindu\.com/reader", r"Hindu.*[Rr]eader"]),
    ("timesgroup", [r"timesgroup\.com", r"epaperbeta", r"timesofindia"]),
    ("jagran", [r"jagran\.com/epaper", r"inextlive"]),
    ("bhaskar", [r"bhaskar\.com", r"dbcorp"]),
    ("amarujala", [r"amarujala\.com"]),
    ("wordpress", [r"wp-content", r"wp-includes"]),
]

# Endpoints worth trying blind. Cheap, and a 200 with JSON is conclusive.
PROBE_PATHS = {
    "summit": [
        "/Home/GetEditionsHierarchy",
        "/Home/GetEditionList",
        "/Home/GetEditionsForSearch",
    ],
    "readwhere": [
        "/api/v1/library",
        "/services/getEditions",
    ],
    "generic": [
        "/api/editions",
        "/api/v1/editions",
        "/api/publications",
        "/Home/GetEditionsHierarchy",
        "/epaper/api/editions",
        "/rss",
        "/feed",
    ],
}


@dataclass
class ProbeResult:
    host: str
    name: str = ""
    reachable: bool = False
    http_status: int = 0
    final_url: str = ""
    platform: str = "unknown"
    platform_confidence: float = 0.0
    signals: list = field(default_factory=list)
    js_assets: list = field(default_factory=list)
    api_endpoints: list = field(default_factory=list)
    working_api: list = field(default_factory=list)
    controller_actions: list = field(default_factory=list)
    notes: str = ""

    def verdict(self) -> str:
        """How this host should be handled by the pipeline."""
        if not self.reachable:
            return "unreachable"
        if self.working_api:
            return "api"          # structured articles available -- best case
        if self.platform != "unknown":
            return "known-platform"  # vendor identified, endpoints need work
        return "cv"               # no API found; page images -> CV pipeline


def _opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))


def fetch(url: str, timeout: int = 25) -> tuple[int, str, str]:
    """GET a URL. Returns (status, final_url, body). Never raises."""
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept-Encoding": "identity"}
    )
    try:
        with _opener().open(req, timeout=timeout) as r:
            return r.status, r.geturl(), r.read(1_500_000).decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception:
        return 0, url, ""


def detect_platform(html: str) -> tuple[str, float, list]:
    """Score HTML against vendor signatures. Returns (platform, confidence, hits)."""
    best, best_hits = "unknown", []
    for platform, patterns in SIGNATURES:
        hits = [p for p in patterns if re.search(p, html, re.I)]
        if len(hits) > len(best_hits):
            best, best_hits = platform, hits
    if not best_hits:
        return "unknown", 0.0, []
    # Confidence scales with how many independent markers matched.
    conf = min(1.0, 0.45 + 0.25 * len(best_hits))
    return best, round(conf, 2), best_hits


def extract_js(html: str, base: str) -> list:
    """Pull script srcs so we can grep the bundles for endpoint names."""
    urls = []
    for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
        u = urllib.parse.urljoin(base, m.group(1))
        if any(x in u for x in ("google", "facebook", "doubleclick", "gtag", "adsby")):
            continue
        urls.append(u)
    return urls[:8]


def mine_actions(text: str) -> list:
    """Find MVC-style controller/action pairs and REST-ish API paths in JS."""
    found = set()
    for m in re.finditer(r'["\'](/?[A-Za-z]{3,20}/[A-Za-z_]{3,30})["\']', text):
        p = m.group(1)
        if re.search(r"\.(js|css|png|jpg|gif|svg|ico|woff)", p, re.I):
            continue
        if p.lower().startswith(("/img/", "/content/", "/scripts/", "/fonts/")):
            continue
        found.add(p if p.startswith("/") else "/" + p)
    for m in re.finditer(r'["\'](/api/[A-Za-z0-9/_.-]{2,60})["\']', text):
        found.add(m.group(1))
    return sorted(found)[:60]


def probe_endpoints(origin: str, platform: str) -> list:
    """Try candidate endpoints; keep the ones that return usable JSON."""
    working = []
    paths = PROBE_PATHS.get(platform, []) + PROBE_PATHS["generic"]
    for path in dict.fromkeys(paths):
        status, _, body = fetch(origin.rstrip("/") + path, timeout=15)
        if status != 200 or not body:
            continue
        stripped = body.lstrip()
        if not stripped.startswith(("[", "{")):
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if isinstance(data, (list, dict)) and data:
            working.append({
                "path": path,
                "type": type(data).__name__,
                "size": len(data),
                "sample": json.dumps(data, ensure_ascii=False)[:200],
            })
    return working


def probe(host: str, name: str = "") -> ProbeResult:
    """Full discovery pass for one e-paper host."""
    url = host if host.startswith("http") else "https://" + host
    res = ProbeResult(host=host, name=name)

    status, final, html = fetch(url)
    res.http_status, res.final_url = status, final
    if status == 0 or not html:
        res.notes = "no response"
        return res
    res.reachable = True

    res.platform, res.platform_confidence, res.signals = detect_platform(html)

    # The homepage rarely names its own endpoints; the JS bundles do.
    res.js_assets = extract_js(html, final)
    blob = html
    for js in res.js_assets:
        _, _, body = fetch(js, timeout=20)
        blob += body
        if len(blob) > 4_000_000:
            break

    if res.platform == "unknown":
        res.platform, res.platform_confidence, res.signals = detect_platform(blob)

    res.controller_actions = mine_actions(blob)
    res.api_endpoints = [a for a in res.controller_actions
                         if re.search(r"(story|article|page|edition|issue|news|api)", a, re.I)]

    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(final))
    res.working_api = probe_endpoints(origin, res.platform)
    return res


def probe_all(targets: list, verbose: bool = True) -> list:
    out = []
    for t in targets:
        host, name = (t if isinstance(t, (list, tuple)) else (t, ""))
        r = probe(host, name)
        out.append(r)
        if verbose:
            print("%-42s %-13s %-6s conf=%.2f api=%d  -> %s"
                  % (host[:42], r.platform, r.http_status,
                     r.platform_confidence, len(r.working_api), r.verdict()),
                  flush=True)
    return out


def save(results: list, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump([{**asdict(r), "verdict": r.verdict()} for r in results],
                  f, ensure_ascii=False, indent=1)
