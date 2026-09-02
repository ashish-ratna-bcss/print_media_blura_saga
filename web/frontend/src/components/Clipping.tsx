import { useEffect, useState } from "react";
import { api } from "../api";
import type { SearchHit } from "../types";
import styles from "./Clipping.module.css";

/**
 * One article as it appeared in the paper.
 *
 * The cut-out is the point. A headline and a paragraph of OCR is a database
 * row; the clipping is what somebody actually reads, and it carries what the
 * text loses -- the size the story ran at, its photograph, the column rules
 * around it. So the image leads and the transcription supports it.
 *
 * Every article in the corpus carries `crop` and `job_id`, which together
 * address the file. Where the image is missing the card still renders from
 * the text, because a story with no picture is still a story.
 *
 * Shared by both Browse modes deliberately. When these were two blocks of
 * JSX, the category list showed the language and the beat and the search list
 * showed a relevance score, and neither showed the date -- differences nobody
 * chose, which is what happens when one thing is written twice.
 */
export function Clipping({
  hit,
  score,
  onTag,
  activeTag,
  children,
}: {
  hit: SearchHit;
  /** Relevance, shown only where the order depends on it. */
  score?: number | null;
  onTag?: (tag: string) => void;
  activeTag?: string;
  children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const src = hit.crop && hit.job_id ? api.mediaUrl(hit.job_id, hit.crop) : null;

  // Escape closes, and the page behind stops scrolling while it is open --
  // scrolling the list under a lightbox reads as the overlay being broken.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [open]);

  return (
    <article className={styles.clip}>
      {src && (
        <button
          type="button"
          className={styles.shot}
          onClick={() => setOpen(true)}
          aria-label={`Enlarge clipping: ${hit.headline || "untitled"}`}
          title="Click to enlarge"
        >
          <img
            src={src}
            alt={hit.headline || "Newspaper clipping"}
            loading="lazy"
            className={styles.img}
          />
        </button>
      )}

      {/* Full size, over the page. Expanding in place pushed everything below
          it down the screen, so reading one clipping lost your place in the
          list you were reading it from. */}
      {open && src && (
        <div
          className={styles.lightbox}
          role="dialog"
          aria-modal="true"
          aria-label={hit.headline || "Newspaper clipping"}
          onClick={() => setOpen(false)}
        >
          <figure className={styles.frame} onClick={(e) => e.stopPropagation()}>
            <img src={src} alt={hit.headline || "Newspaper clipping"} />
            <figcaption>
              <strong>{hit.headline || "(no headline)"}</strong>
              <span>
                {hit.paper}
                {hit.edition ? ` · ${hit.edition}` : ""}
                {hit.date ? ` · ${formatDate(hit.date)}` : ""}
                {hit.page_number ? ` · page ${hit.page_number}` : ""}
              </span>
            </figcaption>
          </figure>
          <button
            type="button"
            className={styles.close}
            onClick={() => setOpen(false)}
            aria-label="Close"
          >
            &times;
          </button>
        </div>
      )}

      <div className={styles.text}>
        <h3 className={styles.headline}>{hit.headline || "(no headline)"}</h3>

        {/* The masthead line, read the way a clipping is filed: which paper,
            which edition, which day, which page. The date is here rather than
            grouped above because a week's results mix days, and a clipping
            without its date is not a clipping. */}
        <p className={styles.slug}>
          <strong>{hit.paper}</strong>
          {hit.edition ? <span> · {hit.edition}</span> : null}
          {hit.date ? <time dateTime={hit.date}> · {formatDate(hit.date)}</time> : null}
          {hit.page_number ? <span> · page {hit.page_number}</span> : null}
        </p>

        <div className={styles.badges}>
          {hit.category && <span className={styles.cat}>{hit.category}</span>}
          {hit.subcategory && <span className={styles.sub}>{hit.subcategory}</span>}
          {hit.byline && <span className={styles.byline}>{hit.byline}</span>}
          {typeof score === "number" && (
            <span className={styles.score}>{score.toFixed(2)}</span>
          )}
        </div>

        {hit.body && <p className={styles.body}>{hit.body.slice(0, 320)}</p>}

        {onTag && hit.tags.length > 0 && (
          <div className={styles.tags}>
            {hit.tags.map((t) => (
              <button
                key={t}
                type="button"
                className={activeTag === t ? styles.tagOn : styles.tag}
                onClick={() => onTag(t)}
              >
                {t}
              </button>
            ))}
          </div>
        )}

        {children}
      </div>
    </article>
  );
}

/** "2026-09-02" -> "Wed 2 Sep 2026". Falls back to the raw string. */
function formatDate(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
