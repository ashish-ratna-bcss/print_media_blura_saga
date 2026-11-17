import { FileUp, Newspaper, Trash2, Wand2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { CatalogueFields } from "../components/CatalogueFields";
import { Dropzone } from "../components/Dropzone";
import { useCatalogue } from "../hooks/useCatalogue";
import type { JobDetail, JobRow } from "../types";
import { ResultView } from "./ResultView";
import styles from "./NewExtractionView.module.css";

export function NewExtractionView({
  onSubmitted,
  onSubmitError,
  job,
  polling,
  detail,
  onOpen,
}: {
  onSubmitted: (jobId: string) => void;
  onSubmitError?: (message: string) => void;
  job: JobRow | null;
  polling: boolean;
  detail: JobDetail | null;
  onOpen: (jobId: string) => void;
}) {
  const [runs, setRuns] = useState<JobRow[]>([]);
  const catalogue = useCatalogue();
  const [pageNo, setPageNo] = useState(1);
  const [withTextCat, setWithTextCat] = useState(true);
  const [withTextUpload, setWithTextUpload] = useState(true);
  const [fileName, setFileName] = useState<string | null>(null);
  const [scope, setScope] = useState<"page" | "range" | "whole">("page");
  const [fromPage, setFromPage] = useState(1);
  const [toPage, setToPage] = useState(8);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const loadRuns = useCallback(() => {
    api
      .jobs(30, "page,upload")
      .then(setRuns)
      .catch(() => setRuns([]));
  }, []);

  useEffect(() => {
    loadRuns();
  }, [loadRuns, job?.status, job?.id]);

  const canExtract =
    !!(catalogue.host && catalogue.titleId && catalogue.date) && !submitting;

  function fail(message: string) {
    setSubmitError(message);
    onSubmitError?.(message);
  }

  async function extractFromCatalogue() {
    setSubmitError("");
    onSubmitError?.("");
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
      fail((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  async function extractFromUpload(file: File) {
    setFileName(file.name);
    setSubmitError("");
    onSubmitError?.("");
    setSubmitting(true);
    try {
      const r = await api.upload(file, withTextUpload);
      onSubmitted(r.job_id);
    } catch (e) {
      fail((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className={styles.stack}>
      <header className={styles.hero}>
        <p className={styles.eyebrow}>Extract</p>
        <h2 className={styles.title}>Pull articles from a newspaper page</h2>
        <p className={styles.lead}>
          Choose a published edition or drop a PDF/image. Layout detection,
          IndicOCR headlines, and Qwen reading run automatically — watch live
          status above while it works.
        </p>
      </header>

      <section className={styles.panel}>
        <div className={styles.panelHead}>
          <span className={styles.panelIcon}>
            <Newspaper size={18} />
          </span>
          <div>
            <h3 className={styles.panelTitle}>From the catalogue</h3>
            <p className={styles.panelSub}>
              Free editions only · last 60 days
            </p>
          </div>
        </div>

        <CatalogueFields
          catalogue={catalogue}
          trailing={
            scope === "page" ? (
              <div className={styles.pageField}>
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

        <div className={styles.scopeRow} role="group" aria-label="How much to extract">
          <span className="field-label">How much</span>
          <div className={styles.scopes}>
            {(
              [
                ["page", "One page"],
                ["range", "Page range"],
                ["whole", "Whole paper"],
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
            Each page is analysed and stored. About a minute per page. Pages
            already held are skipped, so a stopped run can resume.
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
              Read full article text
              <em className={styles.checkNote}>
                Needed for categories and search (~30s per article)
              </em>
            </span>
          </label>
          <button
            type="button"
            className="btn btn-primary"
            disabled={!canExtract}
            onClick={extractFromCatalogue}
          >
            <Wand2 size={14} />
            {submitting ? "Starting…" : "Extract"}
          </button>
        </div>
      </section>

      <div className={styles.divider}>or upload a file</div>

      <section className={styles.panel}>
        <div className={styles.panelHead}>
          <span className={styles.panelIcon}>
            <FileUp size={18} />
          </span>
          <div>
            <h3 className={styles.panelTitle}>Upload PDF or image</h3>
            <p className={styles.panelSub}>
              Same pipeline — layout, OCR, then Qwen
            </p>
          </div>
        </div>
        <Dropzone onFile={extractFromUpload} fileName={fileName} />
        <div className={styles.actions} style={{ marginTop: 14 }}>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={withTextUpload}
              onChange={(e) => setWithTextUpload(e.target.checked)}
            />
            <span>
              Read full article text
              <em className={styles.checkNote}>
                Needed for categories and search (~30s per article)
              </em>
            </span>
          </label>
        </div>
      </section>

      {/* Live status bar (top of app) already shows stage — only show the
          result once the job finishes, so we don't triple-spin. */}
      {detail && detail.job.status === "done" && (
        <ResultView detail={detail} />
      )}
      {detail && detail.job.status === "error" && (
        <ResultView detail={detail} />
      )}

      {runs.length > 0 && (
        <section className={styles.panel}>
          <h3 className={styles.runsTitle}>Your recent extractions</h3>
          <p className={styles.runsSub}>
            Pages and uploads you started. Overnight collections are under Daily.
          </p>
          <div className={styles.tableWrap}>
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
                      {r.edition && (
                        <span className={styles.runEd}> {r.edition}</span>
                      )}
                    </td>
                    <td>{r.page_no || "—"}</td>
                    <td>{r.n_articles}</td>
                    <td>
                      <span
                        className={`${styles.badge} ${styles[`badge_${r.status}`] || ""}`}
                      >
                        {r.status === "running" && r.stage ? r.stage : r.status}
                      </span>
                    </td>
                    <td>
                      <button
                        type="button"
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
        </section>
      )}
    </div>
  );
}
