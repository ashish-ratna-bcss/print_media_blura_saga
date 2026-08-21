import { AlertCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { Catalogue } from "../hooks/useCatalogue";
import { splitEditions, splitPapers } from "../lib/coverage";
import type { Beat } from "../types";
import styles from "./CatalogueFields.module.css";

export function CatalogueFields({
  catalogue: c,
  paperLabel = "Newspaper",
  editionLabel = "Edition",
  dateLabel = "Date",
  beat = null,
  trailing,
}: {
  catalogue: Catalogue;
  paperLabel?: string;
  editionLabel?: string;
  dateLabel?: string;
  /** When set, titles and editions that cover this force are listed first. */
  beat?: Beat | null;
  trailing?: ReactNode;
}) {
  // Grouped rather than filtered. Coverage is a good guess, not ground truth —
  // hiding the rest would make the guess authoritative and leave someone
  // unable to pick a paper they know carries the story.
  const paperGroups = splitPapers(c.papers, beat);
  const editionGroups = splitEditions(c.editions, beat);
  const state = beat?.state ?? "";

  return (
    <div className={styles.row}>
      <div className={styles.field}>
        <label className="field-label" htmlFor="cat-paper">
          {paperLabel}
        </label>
        <div className={styles.selectWrap}>
          <select
            id="cat-paper"
            value={c.host}
            disabled={c.papersLoading}
            onChange={(e) => c.setHost(e.target.value)}
          >
            <option value="">
              {c.papersLoading ? "Loading…" : "Select…"}
            </option>
            {!beat || paperGroups.other.length === 0 ? (
              c.papers.map((p) => (
                <option key={p.host} value={p.host}>
                  {p.name} ({p.lang})
                </option>
              ))
            ) : (
              <>
                <optgroup label={`Circulates in ${state}`}>
                  {paperGroups.covering.map((p) => (
                    <option key={p.host} value={p.host}>
                      {p.name} ({p.lang})
                    </option>
                  ))}
                </optgroup>
                <optgroup label={`Does not cover ${state}`}>
                  {paperGroups.other.map((p) => (
                    <option key={p.host} value={p.host}>
                      {p.name} ({p.lang})
                    </option>
                  ))}
                </optgroup>
              </>
            )}
          </select>
        </div>
        {c.papersError && (
          <div className={styles.error}>
            <AlertCircle size={12} /> {c.papersError}
          </div>
        )}
      </div>

      <div className={styles.field}>
        <label className="field-label" htmlFor="cat-edition">
          {editionLabel}
        </label>
        <div className={styles.selectWrap}>
          <select
            id="cat-edition"
            value={c.titleId}
            disabled={!c.host || c.editionsLoading}
            onChange={(e) => c.setTitleId(e.target.value)}
          >
            <option value="">
              {!c.host ? "Pick a newspaper" : c.editionsLoading ? "Loading…" : "Select…"}
            </option>
            {editionGroups.covering.length > 0 && editionGroups.other.length > 0 ? (
              <>
                <optgroup label={`${state} editions`}>
                  {editionGroups.covering.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.name}
                    </option>
                  ))}
                </optgroup>
                <optgroup label="Other editions">
                  {editionGroups.other.map((e) => (
                    <option key={e.id} value={e.id}>
                      {e.name}
                    </option>
                  ))}
                </optgroup>
              </>
            ) : (
              c.editions.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name}
                </option>
              ))
            )}
          </select>
          {c.editionsLoading && <span className={styles.spinner} />}
        </div>
        {c.editionsError && (
          <div className={styles.error}>
            <AlertCircle size={12} /> {c.editionsError}
          </div>
        )}
      </div>

      <div className={styles.field}>
        <label className="field-label" htmlFor="cat-date">
          {dateLabel}
        </label>
        <div className={styles.selectWrap}>
          <select
            id="cat-date"
            value={c.date}
            disabled={!c.titleId || c.datesLoading}
            onChange={(e) => c.setDate(e.target.value)}
          >
            <option value="">
              {!c.titleId
                ? "Pick an edition"
                : c.datesLoading
                  ? "Loading…"
                  : c.dates.length
                    ? "Select…"
                    : "No free issues"}
            </option>
            {c.dates.map((d) => (
              <option key={d.date} value={d.date}>
                {d.date}
              </option>
            ))}
          </select>
          {c.datesLoading && <span className={styles.spinner} />}
        </div>
        {c.datesError && (
          <div className={styles.error}>
            <AlertCircle size={12} /> {c.datesError}
          </div>
        )}
      </div>

      {trailing}
    </div>
  );
}
