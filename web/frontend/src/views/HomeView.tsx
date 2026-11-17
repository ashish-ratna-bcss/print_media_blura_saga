import {
  CalendarDays,
  FileUp,
  FolderSearch,
  Newspaper,
  ScanSearch,
  Tags,
} from "lucide-react";
import styles from "./HomeView.module.css";

const ACTIONS = [
  {
    id: "extract" as const,
    icon: ScanSearch,
    title: "Extract a page",
    body: "Fetch a free edition or upload a PDF/image. Detects article boxes, OCRs headlines, reads each crop with Qwen.",
  },
  {
    id: "daily" as const,
    icon: Newspaper,
    title: "Daily reader",
    body: "Today’s collected papers and the stories already extracted overnight.",
  },
  {
    id: "browse" as const,
    icon: CalendarDays,
    title: "Browse archive",
    body: "Filter by date, paper, and category across everything stored in MongoDB.",
  },
  {
    id: "categories" as const,
    icon: Tags,
    title: "Manage categories",
    body: "Merge or hide classifier labels so browsing stays clean.",
  },
];

export function HomeView({
  onGo,
}: {
  onGo: (id: "extract" | "daily" | "browse" | "categories") => void;
}) {
  return (
    <div className={styles.wrap}>
      <header className={styles.hero}>
        <p className={styles.eyebrow}>E-Paper</p>
        <h2 className={styles.title}>Newspaper pages → searchable articles</h2>
        <p className={styles.lead}>
          One pipeline: fetch or upload → YOLO layout → IndicOCR headlines →
          Qwen2.5-VL body text → MongoDB. Use Extract to start; Daily and Browse
          to read what you already have.
        </p>
        <button
          type="button"
          className={`btn btn-primary ${styles.heroCta}`}
          onClick={() => onGo("extract")}
        >
          <ScanSearch size={14} /> Start extracting
        </button>
      </header>

      <div className={styles.grid}>
        {ACTIONS.map((a) => {
          const Icon = a.icon;
          return (
            <button
              key={a.id}
              type="button"
              className={styles.card}
              onClick={() => onGo(a.id)}
            >
              <span className={styles.icon}>
                <Icon size={20} strokeWidth={1.8} />
              </span>
              <span className={styles.cardTitle}>{a.title}</span>
              <span className={styles.cardBody}>{a.body}</span>
              <span className={styles.cta}>Open →</span>
            </button>
          );
        })}
      </div>

      <div className={styles.pipe}>
        <FileUp size={16} />
        <span>PDF / page</span>
        <span className={styles.arrow}>→</span>
        <span>YOLO layout</span>
        <span className={styles.arrow}>→</span>
        <span>IndicOCR</span>
        <span className={styles.arrow}>→</span>
        <span>Qwen VL</span>
        <span className={styles.arrow}>→</span>
        <FolderSearch size={16} />
        <span>MongoDB</span>
      </div>
    </div>
  );
}
