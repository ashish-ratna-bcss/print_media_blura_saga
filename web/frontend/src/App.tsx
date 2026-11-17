import {
  CalendarDays,
  House,
  Newspaper,
  ScanSearch,
  Tags,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import styles from "./App.module.css";
import { LiveStatusBar } from "./components/LiveStatusBar";
import { useHealth } from "./hooks/useHealth";
import { useJobPoll } from "./hooks/useJobPoll";
import { BrowseView } from "./views/BrowseView";
import { CategoryAdminView } from "./views/CategoryAdminView";
import { DailyReaderView } from "./views/DailyReaderView";
import { HomeView } from "./views/HomeView";
import { NewExtractionView } from "./views/NewExtractionView";

type TabId = "home" | "extract" | "daily" | "browse" | "categories";

const NAV: { id: TabId; label: string; hint: string; icon: ReactNode }[] = [
  { id: "home", label: "Home", hint: "What you can do", icon: <House size={18} /> },
  {
    id: "extract",
    label: "Extract",
    hint: "Fetch or upload a page",
    icon: <ScanSearch size={18} />,
  },
  {
    id: "daily",
    label: "Daily",
    hint: "Today’s papers",
    icon: <Newspaper size={18} />,
  },
  {
    id: "browse",
    label: "Browse",
    hint: "Search the archive",
    icon: <CalendarDays size={18} />,
  },
  {
    id: "categories",
    label: "Categories",
    hint: "Labels & merges",
    icon: <Tags size={18} />,
  },
];

const TAB_IDS = new Set<string>(NAV.map((n) => n.id));

function tabFromHash(): TabId {
  const raw = (window.location.hash || "").replace(/^#/, "");
  return TAB_IDS.has(raw) ? (raw as TabId) : "home";
}

export default function App() {
  const { health, checking, unreachable } = useHealth(15000);
  const jobPoll = useJobPoll(1200);
  const [tab, setTab] = useState<TabId>(() =>
    typeof window !== "undefined" ? tabFromHash() : "home",
  );
  const [submitError, setSubmitError] = useState("");
  const [statusDismissed, setStatusDismissed] = useState(false);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  function go(id: TabId) {
    setTab(id);
    if (window.location.hash !== `#${id}`) {
      window.location.hash = id;
    }
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function startJob(id: string) {
    setSubmitError("");
    setStatusDismissed(false);
    go("extract");
    jobPoll.watch(id);
  }

  function openRun(id: string) {
    setSubmitError("");
    setStatusDismissed(false);
    go("extract");
    jobPoll.watch(id);
  }

  const job = jobPoll.detail?.job ?? null;
  const showStatus =
    !statusDismissed &&
    (jobPoll.active || !!job || !!submitError);

  const healthLabel = checking
    ? "Checking services…"
    : unreachable
      ? "Backend unreachable"
      : health?.ready
        ? "Pipeline ready"
        : health?.detail || "Not ready";

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <div className={styles.mark}>E</div>
          <div>
            <div className={styles.brandName}>E-Paper</div>
            <div className={styles.brandSub}>Article extraction</div>
          </div>
        </div>

        <nav className={styles.nav} aria-label="Main">
          {NAV.map((item) => (
            <button
              key={item.id}
              type="button"
              className={tab === item.id ? styles.navOn : styles.navBtn}
              aria-current={tab === item.id ? "page" : undefined}
              onClick={() => go(item.id)}
            >
              <span className={styles.navIcon}>{item.icon}</span>
              <span className={styles.navText}>
                <span className={styles.navLabel}>{item.label}</span>
                <span className={styles.navHint}>{item.hint}</span>
              </span>
            </button>
          ))}
        </nav>

        <div className={styles.services}>
          <div className={styles.servicesTitle}>Services</div>
          <ServiceRow
            label="YOLO layout"
            ok={!unreachable && !!health?.layout_ok}
            checking={checking}
          />
          <ServiceRow
            label="IndicOCR"
            ok={!unreachable && !!health?.ocr_ok}
            checking={checking}
          />
          <ServiceRow
            label="Qwen VL"
            ok={!unreachable && !!health?.vlm_ok}
            checking={checking}
          />
          <p className={styles.healthNote}>{healthLabel}</p>
        </div>
      </aside>

      <main className={styles.main} key={tab}>
        {showStatus && (
          <LiveStatusBar
            job={job}
            active={jobPoll.active}
            error={submitError || undefined}
            onDismiss={
              jobPoll.active
                ? undefined
                : () => {
                    setStatusDismissed(true);
                    setSubmitError("");
                  }
            }
          />
        )}

        {tab === "home" && <HomeView onGo={go} />}

        {tab === "extract" && (
          <NewExtractionView
            onSubmitted={startJob}
            onSubmitError={(msg) => {
              setSubmitError(msg);
              setStatusDismissed(false);
            }}
            job={job}
            polling={jobPoll.active}
            detail={jobPoll.detail}
            onOpen={openRun}
          />
        )}

        {tab === "daily" && <DailyReaderView />}

        {tab === "browse" && <BrowseView active />}

        {tab === "categories" && <CategoryAdminView active />}
      </main>
    </div>
  );
}

function ServiceRow({
  label,
  ok,
  checking,
}: {
  label: string;
  ok: boolean;
  checking: boolean;
}) {
  const state = checking ? "checking" : ok ? "ok" : "bad";
  return (
    <div className={styles.svc}>
      <span className={`${styles.dot} ${styles[`dot_${state}`]}`} />
      <span>{label}</span>
    </div>
  );
}
