import { AnimatePresence, motion } from "framer-motion";
import { History, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { EmptyState } from "../components/EmptyState";
import { StatusPill } from "../components/StatusPill";
import { formatSeconds, formatWhen } from "../lib/format";
import type { JobRow } from "../types";
import styles from "./HistoryView.module.css";

export function HistoryView({
  active,
  onOpen,
}: {
  active: boolean;
  onOpen: (jobId: string) => void;
}) {
  const [jobs, setJobs] = useState<JobRow[] | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);

  async function load() {
    try {
      const rows = await api.jobs();
      setJobs(rows);
    } catch {
      setJobs([]);
    }
  }

  useEffect(() => {
    if (active) load();
  }, [active]);

  useEffect(() => {
    if (!confirmId) return;
    const t = setTimeout(() => setConfirmId(null), 3000);
    return () => clearTimeout(t);
  }, [confirmId]);

  async function del(id: string) {
    setJobs((js) => (js ? js.filter((j) => j.id !== id) : js));
    setConfirmId(null);
    try {
      await api.deleteJob(id);
    } catch {
      load();
    }
  }

  if (jobs === null) {
    return (
      <div className={styles.tableWrap}>
        <table>
          <tbody>
            {[...Array(5)].map((_, i) => (
              <tr key={i} className={styles.skeletonRow}>
                {[...Array(9)].map((__, j) => (
                  <td key={j}>
                    <div className={`shimmer ${styles.skeletonBar}`} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (!jobs.length) {
    return (
      <EmptyState
        icon={<History size={20} strokeWidth={1.6} />}
        title="Nothing yet"
        sub="Runs you start from New or Topic Search will show up here."
      />
    );
  }

  return (
    <div className={styles.tableWrap}>
      <div className={styles.scroll}>
        <table>
          <thead>
            <tr>
              <th>When</th>
              <th>Source</th>
              <th>Newspaper</th>
              <th>Edition</th>
              <th>Date</th>
              <th>Pg</th>
              <th>Articles</th>
              <th>Time</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <AnimatePresence initial={false}>
              {jobs.map((j) => (
                <motion.tr
                  key={j.id}
                  className={styles.row}
                  onClick={() => onOpen(j.id)}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0, height: 0 }}
                  transition={{ duration: 0.18 }}
                >
                  <td className="mono">{formatWhen(j.created)}</td>
                  <td>{j.kind === "topic" ? "topic" : j.source}</td>
                  <td className={styles.headline}>{j.paper}</td>
                  <td>{j.edition || "—"}</td>
                  <td className="mono">{j.date || "—"}</td>
                  <td className="mono">{j.kind === "topic" ? "—" : j.page_no || "—"}</td>
                  <td className="mono">{j.n_articles || 0}</td>
                  <td className="mono">{formatSeconds(j.elapsed)}</td>
                  <td>
                    <StatusPill status={j.status} />
                  </td>
                  <td onClick={(e) => e.stopPropagation()}>
                    <button
                      className={`${styles.delBtn} ${confirmId === j.id ? styles.confirm : ""}`}
                      onClick={() => (confirmId === j.id ? del(j.id) : setConfirmId(j.id))}
                    >
                      <Trash2 size={11} />
                      {confirmId === j.id ? "Sure?" : "Delete"}
                    </button>
                  </td>
                </motion.tr>
              ))}
            </AnimatePresence>
          </tbody>
        </table>
      </div>
    </div>
  );
}
