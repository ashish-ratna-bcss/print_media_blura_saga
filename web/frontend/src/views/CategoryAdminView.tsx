import { BookOpen, Merge, Plus, RefreshCw, Shield, Tags } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import type { CategoryRow, Headings } from "../types";
import styles from "./CategoryAdminView.module.css";

/**
 * The category review queue. The housekeeping half of free-form labelling.
 *
 * Categories are not chosen from a fixed list -- the classifier names each article
 * in its own words, so the list describes what the newspapers actually
 * published rather than what somebody predicted. The cost of that is drift:
 * the same subject arrives under several names. Mechanical variants are folded
 * away automatically ("Cyber Crime" and "cyber-crime" are one entry before
 * they reach here); what is left needs a judgement.
 *
 * That judgement is this screen. Sorted by count, because a label used twice
 * is not worth a decision and one used four hundred times is.
 *
 * Merging is reversible: the merged entry stays, pointing at its target, so a
 * label arriving tomorrow still lands in the right place.
 */
export function CategoryAdminView({ active }: { active: boolean }) {
  const [rows, setRows] = useState<CategoryRow[]>([]);
  const [src, setSrc] = useState("");
  const [dst, setDst] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [note, setNote] = useState("");
  const [newLabel, setNewLabel] = useState("");

  /**
   * The fixed scheme, shown for reference.
   *
   * Two different lists live on this screen and conflating them is the
   * confusion it exists to remove: the HEADINGS are decided in advance and
   * are what every article is filed under, while the entries below are what
   * the classifier has actually produced. Somebody correcting an article
   * needs to see the first to make sense of the second.
   */
  const [scheme, setScheme] = useState<Headings | null>(null);
  const [showScheme, setShowScheme] = useState(false);

  useEffect(() => {
    if (!active || scheme) return;
    api.headings().then(setScheme).catch(() => setScheme(null));
  }, [active, scheme]);

  const load = useCallback(() => {
    api
      .categories()
      .then((r) => {
        setRows(r.categories || []);
        if (!r.available && r.error) setErr(r.error);
      })
      .catch((e) => setErr((e as Error).message));
  }, []);

  useEffect(() => {
    if (active) load();
  }, [active, load]);

  async function merge() {
    if (!src || !dst || src === dst) return;
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const r = await api.mergeCategories(src, dst);
      setNote(
        `Filed “${r.merged}” under “${r.into}” — ${r.articles_moved} article${
          r.articles_moved === 1 ? "" : "s"
        } moved.`,
      );
      setSrc("");
      setDst("");
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const label = newLabel.trim();
    if (!label) return;
    setBusy(true);
    setErr("");
    setNote("");
    try {
      const r = await api.createCategory(label);
      setNote(`Added “${r.label}”. It is available now, with nothing filed under it yet.`);
      setNewLabel("");
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggleHidden(row: CategoryRow) {
    setBusy(true);
    try {
      await api.hideCategory(row.id, !row.hidden);
      load();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.stack}>
      <p className={styles.intro}>
        Every category the classifier has produced, most used first. Two entries
        that mean the same thing can be merged — the articles already filed move
        with them, and future articles follow.
      </p>

      {/* The scheme itself. Collapsed by default: it is reference material,
          not the work, and twenty headings above the queue would push the
          thing this screen is for off the first screenful. */}
      {scheme && (
        <section className={styles.scheme}>
          <button
            type="button"
            className={styles.schemeHead}
            aria-expanded={showScheme}
            aria-controls="scheme-body"
            onClick={() => setShowScheme((v) => !v)}
          >
            <span className={styles.schemeTitle}>
              <BookOpen size={14} /> The {scheme.headings.length} headings every
              article is filed under
            </span>
            <span className={styles.schemeToggle}>
              {showScheme ? "hide" : "show"}
            </span>
          </button>

          {showScheme && (
            <div id="scheme-body" className={styles.schemeBody}>
              <p className={styles.langNote}>
                <Shield size={13} /> {scheme.language_note}
              </p>

              {(
                [
                  ["Police desk", true],
                  ["Everything else", false],
                ] as const
              ).map(([group, desk]) => (
                <div key={group} className={styles.group}>
                  <p className={styles.groupName}>{group}</p>
                  <div className={styles.headings}>
                    {scheme.headings
                      .filter((h) => h.desk === desk)
                      .map((h) => (
                        <div
                          key={h.id}
                          className={h.desk ? styles.headingDesk : styles.heading}
                        >
                          <strong>{h.label}</strong>
                          <span className={styles.headingNote}>{h.note}</span>
                          {h.children.length > 0 && (
                            <span className={styles.headingKids}>
                              {h.children.slice(0, 6).join(" · ")}
                              {h.children.length > 6
                                ? ` … ${h.children.length} in all`
                                : ""}
                            </span>
                          )}
                        </div>
                      ))}
                  </div>
                </div>
              ))}

              <p className={styles.schemeFoot}>
                Subcategories under each heading are <em>not</em> fixed — the
                classifier names them in its own words and they appear in the
                list below. The headings are.
              </p>
            </div>
          )}
        </section>
      )}

      {err && <p className={styles.err}>{err}</p>}
      {note && <p className={styles.note}>{note}</p>}

      {/*
        Adding a category by hand. The list normally grows from what the
        classifier produces -- that is the point of free-form labelling -- but
        a heading you know you want should not have to wait for a model to
        think of it, and a merge needs a target that may not exist yet.
      */}
      <div className={styles.mergeBar}>
        <div className={styles.field}>
          <label htmlFor="new-category">Add a category</label>
          <input
            id="new-category"
            className={styles.input}
            value={newLabel}
            placeholder="e.g. municipal waste"
            onChange={(e) => setNewLabel(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void create();
            }}
          />
        </div>
        <button
          type="button"
          className={styles.primary}
          disabled={!newLabel.trim() || busy}
          onClick={create}
        >
          <Plus size={13} /> Add
        </button>
      </div>

      <div className={styles.mergeBar}>
        <div className={styles.field}>
          <label htmlFor="merge-src">File this category</label>
          <select id="merge-src" value={src} onChange={(e) => setSrc(e.target.value)}>
            <option value="">choose…</option>
            {rows.map((r) => (
              <option key={r.id} value={r.id}>
                {r.label} ({r.n})
              </option>
            ))}
          </select>
        </div>
        <div className={styles.field}>
          <label htmlFor="merge-dst">under this one</label>
          <select id="merge-dst" value={dst} onChange={(e) => setDst(e.target.value)}>
            <option value="">choose…</option>
            {rows
              .filter((r) => r.id !== src)
              .map((r) => (
                <option key={r.id} value={r.id}>
                  {r.label} ({r.n})
                </option>
              ))}
          </select>
        </div>
        <button
          type="button"
          className={styles.primary}
          disabled={!src || !dst || src === dst || busy}
          onClick={merge}
        >
          <Merge size={13} /> Merge
        </button>
        <button type="button" className={styles.ghost} onClick={load} disabled={busy}>
          <RefreshCw size={13} /> Refresh
        </button>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          icon={<Tags size={20} />}
          title="No categories yet"
          sub="Categories appear here once the classifier has labelled some articles."
        />
      ) : (
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Category</th>
              <th className={styles.num}>Articles</th>
              <th>Example the model wrote</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className={r.hidden ? styles.hiddenRow : undefined}>
                <td>
                  <span className={styles.name}>{r.label}</span>
                  <span className={styles.id}>{r.id}</span>
                </td>
                <td className={styles.num}>{r.n}</td>
                <td className={styles.raw}>{r.sample_raw || "—"}</td>
                <td className={styles.right}>
                  <button
                    type="button"
                    className={styles.ghost}
                    onClick={() => toggleHidden(r)}
                    disabled={busy}
                    title={
                      r.hidden
                        ? "Show this category in the picker again"
                        : "Keep this category out of the picker without deleting it"
                    }
                  >
                    {r.hidden ? "Show" : "Hide"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
