import {
  AlertTriangle,
  Check,
  Clock,
  Globe,
  Loader2,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useCatalogue } from "../hooks/useCatalogue";
import type { DailyReport, Source } from "../types";
import styles from "./DailyReaderView.module.css";

/**
 * The papers you read every day, and what arrived this morning.
 *
 * Two halves that answer different questions. The report answers "what is
 * there to read today"; the settings answer "what should arrive tomorrow".
 * They sit on one screen because the second is what you reach for the moment
 * the first disappoints you.
 */

const LANGS: Record<string, string> = {
  te: "Telugu",
  hi: "Hindi",
  en: "English",
  ta: "Tamil",
  ml: "Malayalam",
  kn: "Kannada",
  mr: "Marathi",
  bn: "Bengali",
  gu: "Gujarati",
};

const today = () => new Date().toISOString().slice(0, 10);

export function DailyReaderView({ onOpenJob }: { onOpenJob?: (id: string) => void }) {
  const catalogue = useCatalogue();
  const [date, setDate] = useState(today());
  const [report, setReport] = useState<DailyReport | null>(null);
  const [sources, setSources] = useState<Source[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [at, setAt] = useState("05:30");

  const load = useCallback(async () => {
    setError("");
    try {
      const [r, s] = await Promise.all([api.daily(date), api.sources()]);
      setReport(r);
      setSources(s.sources || []);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [date]);

  useEffect(() => {
    void load();
  }, [load]);

  async function collectNow() {
    setBusy("collect");
    setNote("");
    try {
      const r = await api.collectNow(date);
      setNote(
        r.started
          ? `Collecting ${r.due} paper${r.due === 1 ? "" : "s"}: ${(r.papers || []).join(", ")}. This takes minutes per page — reload to see progress.`
          : r.detail || "Nothing due.",
      );
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function toggleSchedule(on: boolean) {
    setBusy("schedule");
    setNote("");
    try {
      const r = await api.setSchedule(on, at);
      setNote(
        on
          ? `Nightly collection registered. Next run ${r.next_run || `at ${at}`}.`
          : "Nightly collection removed. Nothing will run unattended.",
      );
      await load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  async function addCurrent() {
    if (!catalogue.host || !catalogue.titleId) return;
    setBusy("add");
    setError("");
    try {
      await api.addSource({
        host: catalogue.host,
        title_id: String(catalogue.titleId),
        edition: catalogue.edition?.name ?? "",
        lang: catalogue.paper?.lang ?? "",
      });
      await load();
      setNote(`Added ${catalogue.paper?.name ?? catalogue.host}. It will be collected from tomorrow — use Collect now for today.`);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  const sched = report?.schedule;

  return (
    <div className={styles.stack}>
      {/* ------------------------------------------------- today's papers */}
      <div className="card" style={{ padding: 22 }}>
        <div className={styles.head}>
          <div>
            <h2 className={styles.title}>Today&rsquo;s papers</h2>
            <p className={styles.sub}>
              {report
                ? `${report.total} article${report.total === 1 ? "" : "s"} from ${report.collected} of ${report.sources.length} paper${report.sources.length === 1 ? "" : "s"}`
                : "Loading…"}
            </p>
          </div>
          <div className={styles.headActions}>
            <input
              type="date"
              value={date}
              max={today()}
              onChange={(e) => setDate(e.target.value || today())}
              aria-label="Date to show"
            />
            <button className="btn" onClick={() => void load()} disabled={loading}>
              <RefreshCw size={13} /> Refresh
            </button>
            <button className="btn btn-primary" onClick={collectNow} disabled={!!busy}>
              {busy === "collect" ? <Loader2 size={13} className={styles.spin} /> : <Plus size={13} />}
              Collect now
            </button>
          </div>
        </div>

        {error && (
          <div className={`${styles.banner} ${styles.bad}`}>
            <AlertTriangle size={14} />
            <span>{error}</span>
          </div>
        )}
        {note && (
          <div className={`${styles.banner} ${styles.info}`}>
            <Check size={14} />
            <span>{note}</span>
          </div>
        )}

        {report && report.sources.length === 0 && (
          <p className={styles.empty}>
            No papers registered yet. Pick one below and it will be collected every
            morning without being asked.
          </p>
        )}

        <div className={styles.papers}>
          {report?.sources.map((s) => (
            <article
              key={`${s.paper}|${s.edition}`}
              className={`${styles.paper} ${styles[s.status]}`}
            >
              <header>
                <div className={styles.paperName}>
                  <strong>{s.paper || "Unknown"}</strong>
                  {s.edition && <span className={styles.edition}>{s.edition}</span>}
                </div>
                <span className={`${styles.pill} ${styles[`pill_${s.status}`]}`}>
                  {s.status === "collected"
                    ? `${s.n} article${s.n === 1 ? "" : "s"}`
                    : s.status === "waiting"
                      ? "not yet"
                      : "failed"}
                </span>
              </header>

              {s.status === "collected" && (
                <>
                  <p className={styles.meta}>
                    {s.pages.length} page{s.pages.length === 1 ? "" : "s"} · {s.labelled} categorised
                    {s.lang && ` · ${LANGS[s.lang] || s.lang}`}
                  </p>
                  {s.categories.length > 0 && (
                    <div className={styles.cats}>
                      {s.categories.map((c) => (
                        <span key={c} className={styles.cat}>
                          {c}
                        </span>
                      ))}
                    </div>
                  )}
                </>
              )}

              {s.status === "waiting" && (
                <p className={styles.meta}>
                  {s.last_date
                    ? `Last collected ${s.last_date}.`
                    : "Never collected yet."}{" "}
                  Papers usually publish overnight.
                </p>
              )}

              {s.status === "failed" && (
                <p className={styles.metaBad}>
                  {s.last_error || "Collection failed."}
                  {s.fail_count > 1 && ` (${s.fail_count} times in a row)`}
                </p>
              )}

              {!s.registered && (
                <p className={styles.oneOff}>
                  One-off extraction — not part of the daily round.
                </p>
              )}
            </article>
          ))}
        </div>
      </div>

      {/* ------------------------------------------------------- settings */}
      <div className="card" style={{ padding: 22 }}>
        <h2 className={styles.title}>Papers to collect daily</h2>
        <p className={styles.sub}>
          Pick the titles you read. They are fetched, read and categorised every
          morning, so the archive is waiting for you rather than built on demand.
        </p>

        {/* Language first. Somebody who reads Telugu is choosing between two
            titles, not eight, and making them read past six English papers to
            find them is the difference between a setting and a chore. */}
        <div className={styles.picker}>
          <div className={styles.field}>
            <label className="field-label" htmlFor="daily-lang">
              <Globe size={11} /> Language
            </label>
            <select
              id="daily-lang"
              value={catalogue.lang ?? ""}
              onChange={(e) => catalogue.setLang?.(e.target.value)}
            >
              <option value="">All languages</option>
              {[...new Set(catalogue.papers.map((p) => p.lang))].sort().map((l) => (
                <option key={l} value={l}>
                  {LANGS[l] || l}
                </option>
              ))}
            </select>
          </div>

          <div className={styles.field}>
            <label className="field-label" htmlFor="daily-paper">
              Newspaper
            </label>
            <select
              id="daily-paper"
              value={catalogue.host}
              onChange={(e) => catalogue.setHost(e.target.value)}
            >
              <option value="">Select a newspaper</option>
              {catalogue.papers
                .filter((p) => !catalogue.lang || p.lang === catalogue.lang)
                .map((p) => (
                  <option key={p.host} value={p.host}>
                    {p.name} ({LANGS[p.lang] || p.lang})
                  </option>
                ))}
            </select>
          </div>

          <div className={styles.field}>
            <label className="field-label" htmlFor="daily-edition">
              Edition
            </label>
            <select
              id="daily-edition"
              value={catalogue.titleId}
              onChange={(e) => catalogue.setTitleId(e.target.value)}
              disabled={!catalogue.editions.length}
            >
              <option value="">
                {catalogue.editionsLoading ? "Loading…" : "Pick an edition"}
              </option>
              {catalogue.editions.map((ed) => (
                <option key={ed.id} value={String(ed.id)}>
                  {ed.name}
                </option>
              ))}
            </select>
          </div>

          <button
            className="btn btn-primary"
            onClick={addCurrent}
            disabled={!catalogue.host || !catalogue.titleId || !!busy}
          >
            <Plus size={13} /> Add
          </button>
        </div>

        {sources.length > 0 && (
          <table className={styles.table}>
            <thead>
              <tr>
                <th>Newspaper</th>
                <th>Edition</th>
                <th>Language</th>
                <th>Pages</th>
                <th>Last collected</th>
                <th>Daily</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sources.map((s) => (
                <tr key={s.id} className={s.enabled ? "" : styles.off}>
                  <td>
                    <strong>{s.paper}</strong>
                  </td>
                  <td>{s.edition || "—"}</td>
                  <td>{LANGS[s.lang] || s.lang || "—"}</td>
                  <td>
                    {/* 0 is the whole paper. Shown as "all" rather than 0,
                        because a limit of zero pages is what it looks like. */}
                    <input
                      className={styles.pages}
                      type="number"
                      min={0}
                      max={200}
                      value={s.max_pages || 0}
                      aria-label={`Pages to collect from ${s.paper}`}
                      title="0 collects every page the publisher lists"
                      onChange={async (e) => {
                        await api.patchSource(s.id, {
                          max_pages: Number(e.target.value) || 0,
                        });
                        void load();
                      }}
                    />
                    <span className={styles.pagesHint}>
                      {s.max_pages ? `first ${s.max_pages}` : "all"}
                    </span>
                  </td>
                  <td>
                    {s.last_date || "never"}
                    {s.fail_count > 0 && (
                      <span className={styles.failNote}> · {s.fail_count} failures</span>
                    )}
                  </td>
                  <td>
                    <label className={styles.toggle}>
                      <input
                        type="checkbox"
                        checked={s.enabled}
                        onChange={async (e) => {
                          await api.patchSource(s.id, { enabled: e.target.checked });
                          void load();
                        }}
                      />
                      <span>{s.enabled ? "on" : "off"}</span>
                    </label>
                  </td>
                  <td>
                    <button
                      className={styles.iconBtn}
                      aria-label={`Remove ${s.paper}`}
                      onClick={async () => {
                        await api.removeSource(s.id);
                        void load();
                      }}
                    >
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* ------------------------------------------------------ schedule */}
        <div className={styles.schedule}>
          <div className={styles.scheduleText}>
            <strong>
              <Clock size={13} /> Unattended collection
            </strong>
            {sched?.registered ? (
              <span>
                Registered with Windows. Next run{" "}
                <b>{sched.next_run || "—"}</b>
                {sched.last_run && sched.last_run !== "N/A" && (
                  <> · last ran {sched.last_run}</>
                )}
              </span>
            ) : sched?.supported === false ? (
              <span>
                Task Scheduler is not reachable here. Run{" "}
                <code>python scripts/daily.py</code> from cron or any scheduler.
              </span>
            ) : (
              <span>
                Not scheduled. Nothing is collected unless you press Collect now.
              </span>
            )}
          </div>
          <div className={styles.scheduleActions}>
            <input
              type="time"
              value={at}
              onChange={(e) => setAt(e.target.value)}
              aria-label="Time to collect each day"
              disabled={sched?.registered}
            />
            {sched?.registered ? (
              <button className="btn" onClick={() => toggleSchedule(false)} disabled={!!busy}>
                Turn off
              </button>
            ) : (
              <button
                className="btn btn-primary"
                onClick={() => toggleSchedule(true)}
                disabled={!!busy || sched?.supported === false}
              >
                {busy === "schedule" ? (
                  <Loader2 size={13} className={styles.spin} />
                ) : (
                  <Clock size={13} />
                )}
                Collect every day
              </button>
            )}
          </div>
        </div>

        <p className={styles.footnote}>
          The scheduled task runs whether or not this app is open, and survives a
          reboot. Most publishers serve only page 1 without a subscription, so a
          daily round usually collects front pages.
          {onOpenJob && " Each run appears in History."}
        </p>
      </div>
    </div>
  );
}
