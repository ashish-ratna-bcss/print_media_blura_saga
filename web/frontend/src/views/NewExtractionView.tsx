import { Trash2, Wand2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { CatalogueFields } from "../components/CatalogueFields";
import { Dropzone } from "../components/Dropzone";
import { ProgressPanel, type ProgressState } from "../components/ProgressPanel";
import { ResultView } from "./ResultView";
import { useCatalogue } from "../hooks/useCatalogue";
import type { JobDetail, JobRow } from "../types";
import styles from "./NewExtractionView.module.css";

/**
 * Extractions you start by hand, and what they produced.
 *
 * The result and the run list live here rather than in tabs of their own.
 * Extracting and then looking at what you extracted is one activity, and
 * splitting it across three destinations meant the most common next click
 * after pressing Extract was navigating somewhere else.
 *
 * Only `page` and `upload` jobs are listed. Last night's edition collection
 * is the Daily Reader's business; mixing the two would make this a job log
 * rather than a record of what this person did.
 */
export function NewExtractionView({
  onSubmitted,
  job,
  polling,
  detail,
  onOpen,
}: {
  onSubmitted: (jobId: string) => void;
  job: JobRow | null;
  polling: boolean;
  detail: JobDetail | null;
  onOpen: (jobId: string) => void;
}) {
  const [runs, setRuns] = useState<JobRow[]>([]);

  // Reloaded whenever the tracked job changes state, so a run that has just
  // finished appears in the list without needing the tab reopened.
  const loadRuns = useCallback(() => {
    api
      .jobs(30, "page,upload")
      .then(setRuns)
      .catch(() => setRuns([]));
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns, job?.status, job?.id]);

  const catalogue = useCatalogue();
  const [pageNo, setPageNo] = useState(1);
  const [withTextCat, setWithTextCat] = useState(true);
  const [withTextUpload, setWithTextUpload] = useState(true);
  const [fileName, setFileName] = useState<string | null>(null);

  /**
   * How much of the edition to take.
   *
   *   page     one page, the original behaviour
   *   range    pages N to M, for "the city pages"
   *   whole    every page the publisher lists
   *
   * A whole edition is a different job kind (`edition`, not `page`): it is
   * resumable page by page and skips pages already held, which matters when
   * a forty-page paper takes forty minutes.
   */
  const [scope, setScope] = useState<"page" | "range" | "whole">("page");
  const [fromPage, setFromPage] = useState(1);
  const [toPage, setToPage] = useState(8);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const canExtract = !!(catalogue.host && catalogue.titleId && catalogue.date) && !submitting;

  async function extractFromCatalogue() {
    setSubmitError("");
    setSubmitting(true);
    try {
      if (scope === "page") {
        const r = await api.fetchPage({
          host: catalogue.host,
          title_id: catalogue.titleId,
          date: catalogue.date,
          page_no: pageNo || 1,
          edition: catalogue.edition?.name ?? "",
          with_text: withTextCat,
        });
        onSubmitted(r.job_id);
      } else {
        const r = await api.ingestEdition({
          host: catalogue.host,
          title_id: catalogue.titleId,
          date: catalogue.date,
          edition: catalogue.edition?.name ?? "",
          ...(scope === "range"
            ? { from_page: fromPage || 1, to_page: toPage || fromPage || 1 }
            : {}),
        });
        onSubmitted(r.job_id);
      }
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  async function extractFromUpload(file: File) {
    setFileName(file.name);
    setSubmitError("");
    setSubmitting(true);
    try {
      const r = await api.upload(file, withTextUpload);
      onSubmitted(r.job_id);
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  const progressState: ProgressState | null = submitError
    ? "error"
    : job?.status === "done"
      ? "done"
      : job?.status === "error"
        ? "error"
        : job || polling
          ? job?.status === "queued"
            ? "queued"
            : "running"
          : null;

  const progressMessage = submitError
    ? submitError
    : job?.status === "done"
      ? `done in ${(job.elapsed || 0).toFixed(0)}s`
      : job?.status === "error"
        ? `failed: ${job.error || "unknown error"}`
        : undefined;

  return (
    <div className={styles.stack}>
      <div className="card" style={{ padding: 22 }}>
        <CatalogueFields
          catalogue={catalogue}
          trailing={
            scope === "page" ? (
              <div className={`${styles.pageField}`}>
                <label className="field-label" htmlFor="page-no">
                  Page
                </label>
                <input
                  id="page-no"
                  type="number"
                  min={1}
                  max={200}
                  value={pageNo}
                  onChange={(e) => setPageNo(Number(e.target.value) || 1)}
                />
              </div>
            ) : scope === "range" ? (
              <div className={styles.rangeFields}>
                <div className={styles.pageField}>
                  <label className="field-label" htmlFor="from-page">
                    From
                  </label>
                  <input
                    id="from-page"
                    type="number"
                    min={1}
                    max={200}
                    value={fromPage}
                    onChange={(e) => setFromPage(Number(e.target.value) || 1)}
                  />
                </div>
                <div className={styles.pageField}>
                  <label className="field-label" htmlFor="to-page">
                    To
                  </label>
                  <input
                    id="to-page"
                    type="number"
                    min={1}
                    max={200}
                    value={toPage}
                    onChange={(e) => setToPage(Number(e.target.value) || 1)}
                  />
                </div>
              </div>
            ) : null
          }
        />
        {/* How much of the edition. A whole paper is a resumable job that
            skips pages already held, so re-running one is cheap. */}
        <div className={styles.scopeRow} role="group" aria-label="How much to extract">
          <span className="field-label">Extract</span>
          <div className={styles.scopes}>
            {(
              [
                ["page", "One page"],
                ["range", "Page range"],
                ["whole", "Whole newspaper"],
              ] as const
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                className={scope === id ? styles.scopeOn : styles.scope}
                aria-pressed={scope === id}
                onClick={() => setScope(id)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {scope !== "page" && (
          <p className={styles.scopeNote}>
            Every page is analysed, all articles detected, read and categorised,
            then stored. Roughly a minute per page &mdash; a 16-page paper takes
            about a quarter of an hour. Pages already held are skipped, so
            re-running is cheap and a stopped run resumes where it left off.
          </p>
        )}

        <div className={styles.actions}>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={withTextCat}
              onChange={(e) => setWithTextCat(e.target.checked)}
            />
            <span>
              Read the article text
              <em className={styles.checkNote}>
                Needed for categories and search. Adds roughly 30s per article.
              </em>
            </span>
          </label>
          <button className="btn btn-primary" disabled={!canExtract} onClick={extractFromCatalogue}>
            <Wand2 size={13} /> Extract
          </button>
        </div>
        <p className={styles.hint}>
          Only editions that publish without a login are listed. Dates cover the last 60 days.
        </p>
      </div>

      <div className={styles.divider}>or</div>

      <div className="card" style={{ padding: 22 }}>
        <Dropzone onFile={extractFromUpload} fileName={fileName} />
        <div className={styles.actions} style={{ marginTop: 14 }}>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={withTextUpload}
              onChange={(e) => setWithTextUpload(e.target.checked)}
            />
            <span>
              Read the article text
              <em className={styles.checkNote}>
                Needed for categories and search. Adds roughly 30s per article.
              </em>
            </span>
          </label>
        </div>
      </div>

      {progressState && (
        <ProgressPanel state={progressState} stage={job?.stage} message={progressMessage} />
      )}

      {/* The run itself. Shown inline rather than behind a tab: this is what
          pressing Extract was for. */}
      {detail && <ResultView detail={detail} />}

      {runs.length > 0 && (
        <div className="card" style={{ padding: 22 }}>
          <h2 className={styles.runsTitle}>Your extractions</h2>
          <p className={styles.runsSub}>
            Pages and uploads you ran yourself. Papers collected overnight are in
            Daily reader.
          </p>
          <table className={styles.runs}>
            <thead>
              <tr>
                <th>When</th>
                <th>Source</th>
                <th>Page</th>
                <th>Articles</th>
                <th>Status</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  className={styles.runRow}
                  onClick={() => onOpen(r.id)}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onOpen(r.id);
                    }
                  }}
                >
                  <td>{(r.created || "").replace("T", " ").slice(5, 16)}</td>
                  <td>
                    <strong>{r.paper || r.source}</strong>
                    {r.edition && <span className={styles.runEd}> {r.edition}</span>}
                  </td>
                  <td>{r.page_no || "—"}</td>
                  <td>{r.n_articles}</td>
                  <td>
                    <span className={`${styles.badge} ${styles[`badge_${r.status}`] || ""}`}>
                      {r.status}
                    </span>
                  </td>
                  <td>
                    <button
                      className={styles.iconBtn}
                      aria-label={`Delete run ${r.id}`}
                      onClick={async (e) => {
                        e.stopPropagation();
                        await api.deleteJob(r.id);
                        loadRuns();
                      }}
                    >
                      <Trash2 size={13} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
