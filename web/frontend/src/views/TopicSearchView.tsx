import { Search } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { CatalogueFields } from "../components/CatalogueFields";
import { ProgressPanel, type ProgressState } from "../components/ProgressPanel";
import { useCatalogue } from "../hooks/useCatalogue";
import type { JobRow } from "../types";
import styles from "./TopicSearchView.module.css";

const SUGGESTED_TOPICS = [
  "police, crime and arrests",
  "courts and judgments",
  "politics and government",
  "civic issues and infrastructure",
  "health and hospitals",
  "education and schools",
  "sports",
  "business and economy",
  "accidents and disasters",
  "agriculture and farmers",
];

export function TopicSearchView({
  onSubmitted,
  job,
  polling,
}: {
  onSubmitted: (jobId: string) => void;
  job: JobRow | null;
  polling: boolean;
}) {
  const catalogue = useCatalogue();
  const [topic, setTopic] = useState("");
  const [maxPages, setMaxPages] = useState(2);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");

  const canSearch =
    !!(catalogue.host && catalogue.titleId && catalogue.date && topic.trim()) && !submitting;

  async function search() {
    setSubmitError("");
    setSubmitting(true);
    try {
      const r = await api.topicSearch({
        host: catalogue.host,
        title_id: catalogue.titleId,
        date: catalogue.date,
        topic: topic.trim(),
        edition: catalogue.edition?.name ?? "",
        max_pages: maxPages || 2,
      });
      onSubmitted(r.job_id);
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  const note = !catalogue.host
    ? ""
    : catalogue.paper?.platform === "summit"
      ? "Uses the publisher's own article text when that date has it (seconds, exact wording). Otherwise falls back to reading page images — a few minutes per page."
      : "Page images — every article must be found and read first, so allow a few minutes per page.";

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
      ? `found ${job.n_articles} of ${job.searched} in ${(job.elapsed || 0).toFixed(0)}s`
      : job?.status === "error"
        ? `failed: ${job.error || "unknown error"}`
        : undefined;

  return (
    <div className={styles.stack}>
      <div className="card" style={{ padding: 22 }}>
        <CatalogueFields
          catalogue={catalogue}
          trailing={
            <div className={styles.pagesField}>
              <label className="field-label" htmlFor="topic-pages">
                Pages
              </label>
              <input
                id="topic-pages"
                type="number"
                min={1}
                max={60}
                value={maxPages}
                onChange={(e) => setMaxPages(Number(e.target.value) || 1)}
              />
            </div>
          }
        />

        <div style={{ marginTop: 16 }} className={styles.topicField}>
          <label className="field-label" htmlFor="topic-input">
            Topic
          </label>
          <input
            id="topic-input"
            type="text"
            placeholder="e.g. police, crime and arrests"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
          />
          <div className={styles.chips}>
            {SUGGESTED_TOPICS.map((t) => (
              <button key={t} type="button" className={styles.chip} onClick={() => setTopic(t)}>
                {t}
              </button>
            ))}
          </div>
        </div>

        <div className={styles.actions}>
          <p className={styles.note}>{note}</p>
          <button className="btn btn-primary" disabled={!canSearch} onClick={search}>
            <Search size={13} /> Search
          </button>
        </div>
      </div>

      {progressState && (
        <ProgressPanel state={progressState} stage={job?.stage} message={progressMessage} />
      )}
    </div>
  );
}
