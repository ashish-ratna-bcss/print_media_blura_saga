import { Wand2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { CatalogueFields } from "../components/CatalogueFields";
import { Dropzone } from "../components/Dropzone";
import { ProgressPanel, type ProgressState } from "../components/ProgressPanel";
import { useCatalogue } from "../hooks/useCatalogue";
import type { JobRow } from "../types";
import styles from "./NewExtractionView.module.css";

export function NewExtractionView({
  onSubmitted,
  job,
  polling,
}: {
  onSubmitted: (jobId: string) => void;
  job: JobRow | null;
  polling: boolean;
}) {
  const catalogue = useCatalogue();
  const [pageNo, setPageNo] = useState(1);
  const [withTextCat, setWithTextCat] = useState(false);
  const [withTextUpload, setWithTextUpload] = useState(false);
  const [fileName, setFileName] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const canExtract = !!(catalogue.host && catalogue.titleId && catalogue.date) && !submitting;

  async function extractFromCatalogue() {
    setSubmitError("");
    setSubmitting(true);
    try {
      const r = await api.fetchPage({
        host: catalogue.host,
        title_id: catalogue.titleId,
        date: catalogue.date,
        page_no: pageNo || 1,
        edition: catalogue.edition?.name ?? "",
        with_text: withTextCat,
      });
      onSubmitted(r.job_id);
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
          }
        />
        <div className={styles.actions}>
          <label className={styles.check}>
            <input
              type="checkbox"
              checked={withTextCat}
              onChange={(e) => setWithTextCat(e.target.checked)}
            />
            Also read the text (much slower)
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
            Also read the text (much slower)
          </label>
        </div>
      </div>

      {progressState && (
        <ProgressPanel state={progressState} stage={job?.stage} message={progressMessage} />
      )}
    </div>
  );
}
