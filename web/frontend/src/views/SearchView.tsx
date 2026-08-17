import { Database, MapPinOff, RefreshCw, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { BeatPicker } from "../components/BeatPicker";
import type { Beat, CorpusStats, SearchResponse } from "../types";
import styles from "./SearchView.module.css";

/**
 * Search over everything already extracted. The "Archive" tab.
 *
 * Distinct from Topic search, and the difference is worth keeping visible in
 * the UI: Topic search reads a newspaper page to answer one question and costs
 * minutes; this queries what has already been read and costs milliseconds. It
 * can only find what some earlier job indexed.
 *
 * Named Archive in the interface and corpus everywhere behind it -- the API
 * route, the Python module and the database are all still `corpus`. Only the
 * words a reader sees changed.
 */
export function SearchView({ active }: { active: boolean }) {
  const [q, setQ] = useState("");
  const [beat, setBeat] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [beats, setBeats] = useState<Beat[]>([]);
  const [stats, setStats] = useState<CorpusStats | null>(null);
  const [res, setRes] = useState<SearchResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [took, setTook] = useState<number | null>(null);

  const loadStats = useCallback(() => {
    api.corpus().then(setStats).catch(() => setStats(null));
  }, []);

  useEffect(() => {
    if (!active) return;
    loadStats();
    api.beats().then(setBeats).catch(() => setBeats([]));
  }, [active, loadStats]);

  async function run() {
    if (!q.trim()) return;
    setBusy(true);
    setErr("");
    const t0 = performance.now();
    try {
      const r = await api.search({
        q: q.trim(),
        beat: beat || undefined,
        date_from: from || undefined,
        date_to: to || undefined,
        limit: 60,
      });
      setRes(r);
      setTook(Math.round(performance.now() - t0));
    } catch (e) {
      setErr((e as Error).message);
      setRes(null);
    } finally {
      setBusy(false);
    }
  }

  async function backfill() {
    setBusy(true);
    setErr("");
    try {
      const r = await api.backfill();
      setErr(`indexed ${r.added} new and ${r.updated} updated from ${r.jobs} past jobs`);
      loadStats();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const empty = stats && stats.available && stats.articles === 0;

  return (
    <div className={styles.stack}>
      <div className="card" style={{ padding: 22 }}>
        <div className={styles.row}>
          <div className={styles.grow}>
            <label className="field-label" htmlFor="corpus-q">
              Query
            </label>
            <input
              id="corpus-q"
              type="text"
              placeholder="Delhi Police"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run()}
            />
          </div>
          <div className={styles.beatCol}>
            <BeatPicker
              id="corpus-beat"
              beats={beats}
              value={beat}
              onChange={setBeat}
              anyLabel="Any force"
              showHint={false}
            />
          </div>
          <div className={styles.narrow}>
            <label className="field-label" htmlFor="corpus-from">
              From
            </label>
            <input
              id="corpus-from"
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </div>
          <div className={styles.narrow}>
            <label className="field-label" htmlFor="corpus-to">
              To
            </label>
            <input id="corpus-to" type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </div>
        </div>

        <div className={styles.actions}>
          <p className={styles.note}>
            {stats?.available === false ? (
              <span className={styles.bad}>archive unavailable — {stats.error}</span>
            ) : (
              <>
                <Database size={12} /> {stats?.articles ?? 0} articles indexed
                {stats?.earliest ? ` · ${stats.earliest} to ${stats.latest}` : ""}
                {took !== null && res ? ` · last query ${took} ms` : ""}
              </>
            )}
          </p>
          <button className="btn" onClick={backfill} disabled={busy}>
            <RefreshCw size={13} /> Index past jobs
          </button>
          <button className="btn btn-primary" onClick={run} disabled={busy || !q.trim()}>
            <Search size={13} /> Search
          </button>
        </div>

        {err && <p className={styles.err}>{err}</p>}
      </div>

      {empty && (
        <div className="card" style={{ padding: 22 }}>
          <p className={styles.note}>
            Nothing indexed yet. Run an extraction or a topic search, or press{" "}
            <strong>Index past jobs</strong> to pull in articles from runs you have already done.
          </p>
        </div>
      )}

      {res && (
        <div className={styles.results}>
          <p className={styles.summary}>
            {res.total} result{res.total === 1 ? "" : "s"}
            {res.expanded ? (
              <>
                {" "}
                · expanded to <strong>{res.terms}</strong> terms via the {res.beat} vocabulary
              </>
            ) : (
              " · searched as written, no expansion"
            )}
            {res.scanned ? ` · ${res.scanned} candidates scanned` : ""}
          </p>

          {res.results.map((r) => (
            <article key={r.uid} className={styles.hit}>
              <div className={styles.hitTop}>
                <span className={styles.score}>{r.score.toFixed(2)}</span>
                <h3 className={styles.hl}>{r.headline || "(no headline)"}</h3>
              </div>
              <p className={styles.meta}>
                {r.paper}
                {r.edition ? ` · ${r.edition}` : ""} · {r.date} · page {r.page_no} ·{" "}
                <span className={styles.mono}>{r.text_source}</span> · {r.lang}
                {r.off_patch && (
                  <span className={styles.offPatch}>
                    <MapPinOff size={11} /> other region
                  </span>
                )}
              </p>
              {r.body && <p className={styles.body}>{r.body.slice(0, 320)}</p>}
              <div className={styles.chips}>
                <span className={styles.bar} title="lexical vs beat">
                  lex {r.lexical.toFixed(2)} · beat {r.beat_score.toFixed(2)}
                </span>
                {r.keywords.map((k) => (
                  <span key={k} className={styles.kw}>
                    {k}
                  </span>
                ))}
              </div>
              {r.crop && (
                <img
                  className={styles.crop}
                  src={api.mediaUrl(r.job_id, r.crop)}
                  alt={r.headline}
                  loading="lazy"
                />
              )}
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
