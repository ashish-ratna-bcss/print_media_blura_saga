#!/usr/bin/env python3
"""Render out/report.html from the assets built by build_report.py."""
import html
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
A = json.load(open(os.path.join(ROOT, "out/report_assets.json"), encoding="utf-8"))
COV = json.load(open(os.path.join(ROOT, "out/coverage.json"), encoding="utf-8"))

e = lambda s: html.escape(str(s if s is not None else ""))

CSS = """
:root{
  --ground:#F7F8FA; --surface:#FFFFFF; --sunk:#EFF2F7;
  --ink:#131720; --ink-2:#5A6478; --ink-3:#8B94A7;
  --rule:#DCE1EA; --rule-2:#EAEEF4;
  --accent:#1B7FD4; --accent-soft:#E7F1FC;
  --ad:#C9701C; --neutral-class:#767E8E;
  --good:#237A4E; --warn:#B3480E;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#0C1016; --surface:#141A22; --sunk:#10151C;
    --ink:#E7EBF2; --ink-2:#9BA5B7; --ink-3:#6C7686;
    --rule:#242C37; --rule-2:#1C232C;
    --accent:#57ADF0; --accent-soft:#132537;
    --ad:#E3944B; --neutral-class:#7E8797;
    --good:#4FBF89; --warn:#E08A52;
  }
}
:root[data-theme="dark"]{
  --ground:#0C1016; --surface:#141A22; --sunk:#10151C;
  --ink:#E7EBF2; --ink-2:#9BA5B7; --ink-3:#6C7686;
  --rule:#242C37; --rule-2:#1C232C;
  --accent:#57ADF0; --accent-soft:#132537;
  --ad:#E3944B; --neutral-class:#7E8797;
  --good:#4FBF89; --warn:#E08A52;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:var(--sans); font-size:16px; line-height:1.62;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px; margin:0 auto; padding:0 28px 96px}
a{color:var(--accent)}

/* ---- masthead ---- */
.top{padding:64px 0 34px; border-bottom:1px solid var(--rule)}
.eyebrow{
  font-family:var(--mono); font-size:11.5px; letter-spacing:.16em;
  text-transform:uppercase; color:var(--ink-3); margin:0 0 20px;
}
h1{
  font-family:var(--mono); font-weight:600; font-size:clamp(28px,4.4vw,44px);
  line-height:1.12; letter-spacing:-.02em; margin:0 0 16px; text-wrap:balance;
}
.standfirst{
  font-size:18px; color:var(--ink-2); max-width:62ch; margin:0;
}
.statline{
  display:flex; flex-wrap:wrap; gap:0; margin-top:30px;
  border:1px solid var(--rule); border-radius:3px; overflow:hidden;
  background:var(--surface);
}
.stat{
  flex:1 1 150px; padding:14px 18px; border-right:1px solid var(--rule-2);
}
.stat:last-child{border-right:0}
.stat b{
  display:block; font-family:var(--mono); font-size:24px; font-weight:600;
  letter-spacing:-.01em; font-variant-numeric:tabular-nums;
}
.stat span{
  display:block; font-family:var(--mono); font-size:10.5px; letter-spacing:.13em;
  text-transform:uppercase; color:var(--ink-3); margin-top:3px;
}

/* ---- sections ---- */
section{padding-top:58px}
h2{
  font-family:var(--mono); font-size:13px; font-weight:600; letter-spacing:.14em;
  text-transform:uppercase; color:var(--ink-2); margin:0 0 6px;
  padding-bottom:10px; border-bottom:1px solid var(--rule);
}
.lede{font-size:17px; color:var(--ink-2); max-width:64ch; margin:18px 0 0}
p{max-width:64ch}

/* ---- coverage table ---- */
.tablewrap{overflow-x:auto; margin-top:24px; border:1px solid var(--rule); border-radius:3px}
table{border-collapse:collapse; width:100%; background:var(--surface); font-size:14px}
th{
  font-family:var(--mono); font-size:10.5px; letter-spacing:.12em; text-transform:uppercase;
  color:var(--ink-3); text-align:left; font-weight:600;
  padding:11px 14px; border-bottom:1px solid var(--rule); white-space:nowrap;
}
td{padding:9px 14px; border-bottom:1px solid var(--rule-2); vertical-align:middle}
tr:last-child td{border-bottom:0}
td.host,td.num{font-family:var(--mono); font-size:12.5px; color:var(--ink-2)}
td.num{text-align:right; font-variant-numeric:tabular-nums}
.lang{
  font-family:var(--mono); font-size:10px; letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-3); border:1px solid var(--rule); border-radius:2px; padding:1px 5px;
}
.pill{
  display:inline-block; font-family:var(--mono); font-size:10.5px; letter-spacing:.07em;
  text-transform:uppercase; padding:2px 8px; border-radius:2px; white-space:nowrap;
  border:1px solid currentColor;
}
.pill.live{color:var(--good)}
.pill.cv{color:var(--accent)}
.pill.dead{color:var(--ink-3)}
.has{color:var(--good); font-weight:600}

/* ---- page spread ---- */
.spread{
  margin-top:32px; border:1px solid var(--rule); border-radius:3px;
  background:var(--surface); overflow:hidden;
}
.spread-head{
  display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px;
  padding:16px 20px; border-bottom:1px solid var(--rule); background:var(--sunk);
}
.spread-head h3{
  font-family:var(--mono); font-size:15px; font-weight:600; margin:0; letter-spacing:-.01em;
}
.spread-head .meta{
  font-family:var(--mono); font-size:11.5px; color:var(--ink-3);
  font-variant-numeric:tabular-nums; letter-spacing:.04em;
}
.spread-body{display:grid; grid-template-columns:minmax(0,1fr); gap:0}
@media(min-width:860px){ .spread-body{grid-template-columns:minmax(0,420px) minmax(0,1fr)} }
.viewer{padding:20px; border-bottom:1px solid var(--rule); background:var(--sunk)}
@media(min-width:860px){ .viewer{border-bottom:0; border-right:1px solid var(--rule)} }
.frame{position:relative; border:1px solid var(--rule); background:var(--surface); line-height:0}
.frame img{width:100%; height:auto; display:block}
.frame img.off{display:none}
.toggle{display:flex; gap:0; margin:0 0 12px; border:1px solid var(--rule); border-radius:3px; overflow:hidden; width:fit-content}
.toggle button{
  font-family:var(--mono); font-size:11px; letter-spacing:.1em; text-transform:uppercase;
  padding:7px 14px; border:0; background:var(--surface); color:var(--ink-2); cursor:pointer;
  border-right:1px solid var(--rule);
}
.toggle button:last-child{border-right:0}
.toggle button[aria-pressed="true"]{background:var(--accent); color:#fff}
.toggle button:focus-visible{outline:2px solid var(--accent); outline-offset:-2px}
.legend{display:flex; flex-wrap:wrap; gap:14px; margin-top:12px}
.legend span{font-family:var(--mono); font-size:10.5px; letter-spacing:.08em; text-transform:uppercase; color:var(--ink-3); display:flex; align-items:center; gap:6px}
.sw{width:11px; height:11px; border-radius:2px; display:inline-block}

/* ---- clippings ---- */
.clips{padding:20px}
.clips-title{
  font-family:var(--mono); font-size:10.5px; letter-spacing:.14em; text-transform:uppercase;
  color:var(--ink-3); margin:0 0 14px;
}
.grid{display:grid; grid-template-columns:repeat(auto-fill,minmax(220px,1fr)); gap:16px}
.clip{border:1px solid var(--rule); border-radius:3px; overflow:hidden; background:var(--surface); display:flex; flex-direction:column}
.clip figure{margin:0; background:var(--sunk); border-bottom:1px solid var(--rule-2); line-height:0}
.clip img{width:100%; height:auto; display:block}
.clip figcaption{padding:11px 12px 12px; display:flex; flex-direction:column; gap:7px}
.clip .hl{font-size:13.5px; line-height:1.35; font-weight:600; color:var(--ink); margin:0}
.clip .kick{font-size:11.5px; line-height:1.35; color:var(--ink-3); margin:0}
.chips{display:flex; flex-wrap:wrap; gap:5px; margin-top:2px}
.chip{
  font-family:var(--mono); font-size:9.5px; letter-spacing:.07em; text-transform:uppercase;
  color:var(--ink-3); border:1px solid var(--rule); border-radius:2px; padding:1px 5px;
  font-variant-numeric:tabular-nums;
}
.chip.cont{color:var(--accent); border-color:var(--accent)}
.chip.pkg{color:var(--ad); border-color:var(--ad)}
.seq{
  font-family:var(--mono); font-size:10px; color:#fff; background:var(--accent);
  border-radius:2px; padding:1px 6px; letter-spacing:.06em; align-self:flex-start;
}

/* ---- notes ---- */
.notes{margin-top:26px; display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.note{border:1px solid var(--rule); border-left:2px solid var(--accent); border-radius:3px; padding:15px 17px; background:var(--surface)}
.note.caution{border-left-color:var(--warn)}
.note h4{font-family:var(--mono); font-size:11px; letter-spacing:.11em; text-transform:uppercase; margin:0 0 7px; color:var(--ink-2)}
.note p{margin:0; font-size:14px; color:var(--ink-2); max-width:none}
code{font-family:var(--mono); font-size:.88em; background:var(--sunk); border:1px solid var(--rule-2); border-radius:2px; padding:1px 4px}
footer{margin-top:64px; padding-top:20px; border-top:1px solid var(--rule); font-family:var(--mono); font-size:11px; letter-spacing:.06em; color:var(--ink-3)}
@media (prefers-reduced-motion:reduce){*{transition:none!important; animation:none!important}}
"""

JS = """
document.querySelectorAll('.toggle').forEach(function(t){
  t.addEventListener('click', function(ev){
    var b = ev.target.closest('button'); if(!b) return;
    var box = t.parentElement;
    t.querySelectorAll('button').forEach(function(x){ x.setAttribute('aria-pressed', String(x===b)); });
    box.querySelectorAll('.frame img').forEach(function(img){
      img.classList.toggle('off', img.dataset.view !== b.dataset.view);
    });
  });
});
"""


def coverage_rows():
    order = {"known-platform": 0, "cv": 1, "unreachable": 2}
    rows = sorted(COV, key=lambda r: (order.get(r["verdict"], 3), -r["pages"], r["name"]))
    out = []
    for r in rows:
        if r["pages"]:
            pill = '<span class="pill live">fetching</span>'
        elif r["verdict"] == "unreachable":
            pill = '<span class="pill dead">host down</span>'
        else:
            pill = '<span class="pill cv">needs work</span>'
        pages = ('<span class="has">%d</span>' % r["pages"]) if r["pages"] else "&mdash;"
        out.append(
            "<tr><td>%s</td><td><span class=\"lang\">%s</span></td>"
            "<td class=\"host\">%s</td><td class=\"host\">%s</td>"
            "<td>%s</td><td class=\"num\">%s</td></tr>"
            % (e(r["name"]), e(r["lang"]), e(r["host"]),
               e(r["platform"] if r["platform"] != "unknown" else "—"), pill, pages))
    return "\n".join(out)


def clip_card(c):
    chips = ['<span class="chip">%s col</span>' % e(c["columns"])] if c["columns"] else []
    if c["photo"]:
        chips.append('<span class="chip">photo</span>')
    chips.append('<span class="chip">%s px</span>' % e(c["px"]))
    if c["cont"]:
        chips.append('<span class="chip cont">cont. p%s</span>' % e(c["cont"]))
    if c.get("package"):
        chips.append('<span class="chip pkg">umbrella</span>')
    kick = '<p class="kick">%s</p>' % e(c["kicker"]) if c["kicker"] else ""
    return ("""<article class="clip">
  <figure>
    <img src="%s" alt="Clipping %s: %s" loading="lazy">
    <figcaption>
      <span class="seq">a%02d</span>
      %s<p class="hl">%s</p>
      <div class="chips">%s</div>
    </figcaption>
  </figure>
</article>""" % (c["img"], c["seq"], e(c["headline"])[:80], c["seq"], kick,
                 e(c["headline"]), "".join(chips)))


def spread(p):
    lang = "Hindi" if p["lang"] == "hi" else "English"
    return ("""<div class="spread">
  <div class="spread-head">
    <h3>%s &mdash; %s</h3>
    <span class="meta">page %s &middot; %s &middot; %s &middot; %s px</span>
  </div>
  <div class="spread-body">
    <div class="viewer">
      <div class="toggle" role="group" aria-label="Page view">
        <button type="button" data-view="raw" aria-pressed="false">Page</button>
        <button type="button" data-view="ann" aria-pressed="true">Regions</button>
      </div>
      <div class="frame">
        <img data-view="raw" class="off" src="%s" alt="%s page %s as printed">
        <img data-view="ann" src="%s" alt="%s page %s with detected regions outlined">
      </div>
      <div class="legend">
        <span><i class="sw" style="background:#1B7FD4"></i>article &times;%d</span>
        <span><i class="sw" style="background:#C9701C"></i>advert &times;%d</span>
        <span><i class="sw" style="background:#767E8E"></i>masthead</span>
      </div>
    </div>
    <div class="clips">
      <p class="clips-title">%d clippings cut from this page</p>
      <div class="grid">
%s
      </div>
    </div>
  </div>
</div>""" % (e(p["paper"]), e(p["edition"]), e(p["page"]), e(p["date"]), lang, e(p["size"]),
             p["raw"], e(p["paper"]), e(p["page"]),
             p["annotated"], e(p["paper"]), e(p["page"]),
             p["n_article"], p["n_ad"],
             len(p["clips"]), "\n".join(clip_card(c) for c in p["clips"])))


total_clips = sum(len(p["clips"]) for p in A["pages"])
langs = sorted({("Hindi" if p["lang"] == "hi" else "English") for p in A["pages"]})

HTML = """<title>Delhi Clipping Desk</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>%s</style>
<div class="wrap">

<header class="top">
  <p class="eyebrow">Extraction run &middot; 12 August 2026</p>
  <h1>Every article, cut from the page it was printed on</h1>
  <p class="standfirst">Today's Delhi editions, acquired and pulled apart into individual
  news items. The test that matters is whether the same code survives a change of layout
  and a change of script &mdash; so it was run on an English broadsheet and a Hindi one,
  unchanged.</p>
  <div class="statline">
    <div class="stat"><b>35</b><span>Delhi titles mapped</span></div>
    <div class="stat"><b>113</b><span>pages acquired today</span></div>
    <div class="stat"><b>%d</b><span>clippings cut</span></div>
    <div class="stat"><b>%s</b><span>scripts handled</span></div>
  </div>
</header>

<section>
  <h2>What is actually published in Delhi</h2>
  <p class="lede">Thirty-five dailies circulate in Delhi-NCR across English, Hindi and Urdu.
  Every one was probed for an article-level API. <strong>None has one.</strong> That is the
  finding that sets the architecture: unlike some regional publishers, no Delhi paper hands
  you its articles, so boundaries have to be recovered from the page itself.</p>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Title</th><th>Lang</th><th>E-paper host</th><th>Platform</th><th>Status</th><th>Pages today</th></tr></thead>
      <tbody>
%s
      </tbody>
    </table>
  </div>
  <div class="notes">
    <div class="note"><h4>Fetching today</h4><p>Five titles on the Readwhere platform
    deliver complete editions with no login &mdash; Financial Express 54 pages, Jansatta 44,
    The Statesman 12.</p></div>
    <div class="note caution"><h4>Paywalled</h4><p>The Tribune and Dainik Tribune serve
    page&nbsp;1 free and gate the rest. <code>pagemeta/get</code> accepts a subscriber
    token, so paid accounts unlock full editions.</p></div>
    <div class="note caution"><h4>Needs a parser</h4><p>Nine hosts respond but use bespoke
    viewers (Times, HT, Jagran, Amar Ujala); eleven have moved or retired their host.</p></div>
  </div>
</section>

<section>
  <h2>Two pages, one pipeline</h2>
  <p class="lede">Boundaries are established before a single word is read. The layout pass
  gets the whole page and is asked only <em>where</em> each article sits &mdash; gutters,
  rules and whitespace tell it that, so no transcription is involved. Only then is each
  region cut from the full-resolution page and read on its own. Sending one article per
  frame is what stops a neighbouring column from bleeding in, which is the mechanism behind
  headline-A-with-body-B.</p>
%s
</section>

<section>
  <h2>Does it hold when the layout changes</h2>
  <p class="lede">The two pages above share no structural assumption. The Tribune is a
  6-column English broadsheet with a lead package and a full-width advert. Jansatta is a
  5-column Hindi paper with twelve items, a signed cartoon, boxed sidebars and an umbrella
  headline sitting over three sibling stories. The layout file changes; the code does not.
  Layout and language are data here, not branches.</p>
  <div class="notes">
    <div class="note"><h4>Script independence</h4><p>Nothing in the segmentation stage is
    language-specific. Devanagari and Latin pages run identically because the layout pass
    reasons about geometry, not words.</p></div>
    <div class="note caution"><h4>Umbrella packages</h4><p>Jansatta's FCRA block is one
    headline over three separate stories. It is marked <code>package</code> rather than
    silently split &mdash; guessing here is exactly how content gets mismatched.</p></div>
    <div class="note caution"><h4>Boundaries need padding</h4><p>A box 0.3%% too narrow clips
    a column mid-word; one 1%% too tall pulls in a line from the story above. Both occurred
    on the first pass and were caught by looking at the crops.</p></div>
  </div>
</section>

<section>
  <h2>Where this stands</h2>
  <p class="lede">Acquisition, layout, cropping and storage run end to end today; the 113
  pages and their records are in MongoDB. The remaining stage is transcription &mdash;
  reading each clipping into headline, byline, dateline and body text. It needs an API key,
  which this run did not have, so every clipping here is proven as a boundary rather than
  as text.</p>
  <div class="notes">
    <div class="note"><h4>Done</h4><p>Platform discovery across 35 titles, full-edition
    acquisition at up to 2400&times;3694, two-layer page compositing, region detection,
    clipping, MongoDB persistence.</p></div>
    <div class="note caution"><h4>Next</h4><p>Set <code>ANTHROPIC_API_KEY</code> to turn
    clippings into structured text; add subscriber tokens for the paywalled titles; write
    per-theme parsers for the Times, HT, Jagran and Amar Ujala viewers.</p></div>
  </div>
</section>

<footer>Namasté Telangana &middot; The Tribune &middot; Jansatta &middot; Financial Express &middot; The Statesman &mdash; extracted 12 August 2026. Page images remain the property of their publishers.</footer>
</div>
<script>%s</script>
""" % (CSS, total_clips, " + ".join(langs), coverage_rows(),
       "\n".join(spread(p) for p in A["pages"]), JS)

dest = os.path.join(ROOT, "out/report.html")
open(dest, "w", encoding="utf-8").write(HTML)
print("wrote %s (%.1f MB)" % (dest, os.path.getsize(dest) / 1e6))
