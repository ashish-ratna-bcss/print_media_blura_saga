import { motion } from "framer-motion";
import { ImageOff } from "lucide-react";
import { api } from "../api";
import type { ArticleRow } from "../types";
import styles from "./ClipCard.module.css";

const cardVariants = {
  hidden: { opacity: 0, y: 14, scale: 0.98 },
  show: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: 0.32, ease: [0.22, 0.9, 0.32, 1] as const },
  },
};

export function ClipCard({
  jobId,
  article,
  onZoom,
}: {
  jobId: string;
  article: ArticleRow;
  onZoom: (src: string, alt: string) => void;
}) {
  const src = article.crop ? api.mediaUrl(jobId, article.crop) : null;
  const isAd = article.type === "ad";
  const bylineLine = [article.byline, article.dateline].filter(Boolean).join(" · ");

  return (
    <motion.article
      variants={cardVariants}
      whileHover={{ y: -3 }}
      className={`${styles.card} ${isAd ? styles.ad : ""}`}
    >
      <figure className={styles.figure}>
        {src ? (
          <img
            src={src}
            alt={`clipping ${article.seq}`}
            loading="lazy"
            onClick={() => onZoom(src, article.headline || `Clipping ${article.seq}`)}
          />
        ) : (
          <div className={styles.noImage}>
            <ImageOff size={22} strokeWidth={1.5} />
          </div>
        )}
      </figure>
      <div className={styles.body}>
        <p className={`${styles.hl} ${!article.headline ? styles.hlEmpty : ""}`}>
          {article.headline || "(no headline read)"}
        </p>
        {bylineLine && <p className={styles.meta}>{bylineLine}</p>}

        {article.topic_score != null && (
          <div className={styles.score}>
            <span>{article.topic_score.toFixed(2)}</span>
            <span className={styles.scoreTrack}>
              <span
                className={styles.scoreFill}
                style={{ width: `${Math.round(article.topic_score * 100)}%` }}
              />
            </span>
          </div>
        )}
        {article.topic_why && <p className={styles.why}>{article.topic_why}</p>}
        {(article.topic_summary || article.body) && (
          <div className={styles.txtWrap}>
            <p className={styles.txt}>
              {(article.topic_summary || article.body).slice(0, 360)}
            </p>
          </div>
        )}

        <div className={styles.chips}>
          <span className={`${styles.chip} ${isAd ? styles.ad : styles.type}`}>
            {article.type}
          </span>
          {!!article.n_elements && <span className={styles.chip}>{article.n_elements} blocks</span>}
          {!!article.has_photo && <span className={styles.chip}>photo</span>}
          {article.category && <span className={styles.chip}>{article.category}</span>}
          {article.page_ref != null && article.page_ref > 0 && (
            <span className={styles.chip}>page {article.page_ref}</span>
          )}
        </div>
      </div>
    </motion.article>
  );
}
