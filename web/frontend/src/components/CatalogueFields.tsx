import { AlertCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { Catalogue } from "../hooks/useCatalogue";
import styles from "./CatalogueFields.module.css";

export function CatalogueFields({
  catalogue: c,
  paperLabel = "Newspaper",
  editionLabel = "Edition",
  dateLabel = "Date",
  trailing,
}: {
  catalogue: Catalogue;
  paperLabel?: string;
  editionLabel?: string;
  dateLabel?: string;
  trailing?: ReactNode;
}) {
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
            {c.papers.map((p) => (
              <option key={p.host} value={p.host}>
                {p.name} ({p.lang})
              </option>
            ))}
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
            {c.editions.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
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
