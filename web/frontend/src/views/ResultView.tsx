import { motion } from "framer-motion";
import { AlertTriangle, FileSearch2, Loader2 } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import { ClipCard } from "../components/ClipCard";
import { ClipGrid } from "../components/ClipGrid";
import { EmptyState } from "../components/EmptyState";
import { StatRow, type Stat } from "../components/StatRow";
import { ZoomModal, type ZoomTarget } from "../components/ZoomModal";
import type { JobDetail } from "../types";
import styles from "./ResultView.module.css";

export function ResultView({ detail }: { detail: JobDetail | null }) {
  const [zoom, setZoom] = useState<ZoomTarget | null>(null);

  if (!detail) {
    return (
      <EmptyState
        icon={<FileSearch2 size={20} strokeWidth={1.6} />}
        title="No extraction selected yet"
        sub="Run a new extraction or a topic search, or open a past run from History."
      />
    );
  }

  if (detail.job.status === "error") {
    return (
      <EmptyState
        icon={<AlertTriangle size={20} strokeWidth={1.6} />}
        title="This run failed"
        sub={detail.job.error || "Unknown error."}
      />
    );
  }

  if (detail.job.status !== "done") {
    return (
      <EmptyState
        icon={<Loader2 size={20} strokeWidth={1.6} className="spin" />}
        title="Still processing…"
        sub={detail.job.stage || detail.job.status}
      />
    );
  }

  const { job, articles } = detail;

  if (job.kind === "topic") {
    const stats: Stat[] = [
      { value: articles.length, label: "matches" },
      { value: job.searched || 0, label: "articles searched" },
      { value: job.screened || 0, label: "shortlisted" },
      { value: job.elapsed || 0, label: "elapsed", suffix: "s" },
    ];
    return (
      <motion.div
        className={styles.stack}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ duration: 0.2 }}
      >
        <StatRow stats={stats} />
        <p className={styles.topicHeader}>
          &ldquo;<strong>{job.topic}</strong>&rdquo; in {job.paper} {job.edition || ""} ·{" "}
          {job.date}
        </p>
        {articles.length ? (
          <ClipGrid>
            {articles.map((a) => (
              <ClipCard key={a.seq} jobId={job.id} article={a} onZoom={(src, alt) => setZoom({ src, alt })} />
            ))}
          </ClipGrid>
        ) : (
          <EmptyState
            icon={<FileSearch2 size={20} strokeWidth={1.6} />}
            title="No stories matched"
            sub="Try a broader topic, or search more pages."
          />
        )}
        <ZoomModal target={zoom} onClose={() => setZoom(null)} />
      </motion.div>
    );
  }

  const stats: Stat[] = [
    { value: job.n_articles, label: "articles" },
    { value: job.n_ads, label: "adverts" },
    { value: (job.coverage || 0) * 100, label: "page covered", suffix: "%", decimals: 0 },
    { value: job.elapsed || 0, label: "elapsed", suffix: "s" },
    { raw: `${job.width || 0}×${job.height || 0}`, label: "source px" },
  ];

  return (
    <motion.div
      className={styles.stack}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.2 }}
    >
      <StatRow stats={stats} />
      <div className={styles.split}>
        <div className={`card ${styles.viewerCard}`}>
          <div className={styles.frame}>
            <img
              src={api.mediaUrl(job.id, "overlay.jpg")}
              alt="page with detected articles outlined"
              onClick={() =>
                setZoom({ src: api.mediaUrl(job.id, "overlay.jpg"), alt: "Detected regions" })
              }
            />
          </div>
          <div className={styles.legend}>
            <span>
              <i className={styles.sw} style={{ background: "var(--accent)" }} /> article
            </span>
            <span>
              <i className={styles.sw} style={{ background: "var(--ad)" }} /> advert
            </span>
            <span>
              <i className={styles.sw} style={{ background: "var(--text-3)" }} /> masthead
            </span>
          </div>
          <p className={styles.caption}>
            {job.paper} {job.edition && `· ${job.edition}`} {job.date && `· ${job.date}`} · page{" "}
            {job.page_no || 1}
          </p>
        </div>
        <ClipGrid>
          {articles.map((a) => (
            <ClipCard key={a.seq} jobId={job.id} article={a} onZoom={(src, alt) => setZoom({ src, alt })} />
          ))}
        </ClipGrid>
      </div>
      <ZoomModal target={zoom} onClose={() => setZoom(null)} />
    </motion.div>
  );
}
