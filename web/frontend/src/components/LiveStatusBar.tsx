import { AlertTriangle, CheckCircle2, Loader2, Radio, X } from "lucide-react";
import { parseProgress } from "../lib/format";
import type { JobRow } from "../types";
import styles from "./LiveStatusBar.module.css";

/**
 * Sticky extraction status so stage updates stay visible while a long
 * YOLO → IndicOCR → Qwen job runs.
 */
export function LiveStatusBar({
  job,
  active,
  error,
  onDismiss,
}: {
  job: JobRow | null;
  active: boolean;
  error?: string;
  onDismiss?: () => void;
}) {
  if (!active && !job && !error) return null;

  const status = error
    ? "error"
    : job?.status === "done"
      ? "done"
      : job?.status === "error"
        ? "error"
        : active || job
          ? job?.status === "queued"
            ? "queued"
            : "running"
          : null;

  if (!status) return null;

  const stage = (job?.stage || "").trim();
  const title =
    error ||
    (status === "done"
      ? `Finished in ${(job?.elapsed || 0).toFixed(0)}s · ${job?.n_articles || 0} articles`
      : status === "error"
        ? job?.error || "Extraction failed"
        : stage || (status === "queued" ? "Queued…" : "Starting…"));

  const meta = [
    job?.paper || job?.source,
    job?.edition,
    job?.page_no ? `p${job.page_no}` : "",
    job?.id ? `#${job.id.slice(0, 8)}` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const frac =
    status === "running" || status === "queued" ? parseProgress(stage) : null;

  return (
    <div className={`${styles.wrap} ${styles[status]}`} role="status" aria-live="polite">
      <div className={styles.bar}>
        <span className={styles.icon}>
          {(status === "running" || status === "queued") && (
            <Loader2 size={18} className={styles.spin} />
          )}
          {status === "done" && <CheckCircle2 size={18} />}
          {status === "error" && <AlertTriangle size={18} />}
        </span>
        <div className={styles.body}>
          <div className={styles.title}>{title}</div>
          {meta && <div className={styles.meta}>{meta}</div>}
          {(status === "running" || status === "queued") && (
            <div className={styles.track}>
              <span
                className={`${styles.fill} ${frac === null ? styles.indeterminate : ""}`}
                style={frac === null ? undefined : { width: `${Math.round(frac * 100)}%` }}
              />
            </div>
          )}
        </div>
        {(status === "running" || status === "queued") && (
          <span className={styles.live}>
            <Radio size={12} /> Live
          </span>
        )}
        {onDismiss && (
          <button
            type="button"
            className={styles.dismiss}
            aria-label="Dismiss status"
            onClick={onDismiss}
          >
            <X size={16} />
          </button>
        )}
      </div>
    </div>
  );
}
