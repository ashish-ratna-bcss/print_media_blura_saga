import { motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import type { ReactNode } from "react";
import { parseProgress } from "../lib/format";
import styles from "./ProgressPanel.module.css";

export type ProgressState = "queued" | "running" | "done" | "error";

export function ProgressPanel({
  state,
  stage,
  message,
}: {
  state: ProgressState;
  stage?: string | null;
  message?: ReactNode;
}) {
  const frac = state === "running" || state === "queued" ? parseProgress(stage) : null;

  return (
    <motion.div
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      className={`${styles.bar} ${styles[state]}`}
    >
      <span className={styles.icon}>
        {(state === "queued" || state === "running") && (
          <Loader2 size={16} className={styles.spin} />
        )}
        {state === "done" && <CheckCircle2 size={16} />}
        {state === "error" && <AlertTriangle size={16} />}
      </span>
      <span className={styles.text}>
        <span className={styles.label}>{message ?? stage ?? state}</span>
        {(state === "running" || state === "queued") && (
          <span className={styles.track}>
            <span
              className={`${styles.fill} ${frac === null ? styles.indeterminate : ""}`}
              style={frac === null ? undefined : { width: `${Math.round(frac * 100)}%` }}
            />
          </span>
        )}
      </span>
    </motion.div>
  );
}
