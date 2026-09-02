import { CalendarDays, Plus, Sunrise, Tags } from "lucide-react";
import { useState, type ReactNode } from "react";
import styles from "./App.module.css";
import { Header } from "./components/Header";
import { TabBar, type TabDef } from "./components/TabBar";
import { useHealth } from "./hooks/useHealth";
import { useJobPoll } from "./hooks/useJobPoll";
import type { JobKind } from "./types";
import { BrowseView } from "./views/BrowseView";
import { DailyReaderView } from "./views/DailyReaderView";
import { NewExtractionView } from "./views/NewExtractionView";
import { CategoryAdminView } from "./views/CategoryAdminView";

// "archive" is the UI's name for what the backend calls the corpus. The
// distinction is deliberate: /api/corpus, epaper/corpus.py and the
// epaper_corpus database keep the technical term, while the tab says the
// thing a reader would say.
type TabId = "new" | "daily" | "browse" | "categories";

function TabPanel({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <section hidden={!active} className={active ? styles.panelActive : styles.panel}>
      {children}
    </section>
  );
}

export default function App() {
  const { health, checking, unreachable } = useHealth();
  const jobPoll = useJobPoll();
  const [activeTab, setActiveTab] = useState<TabId>("new");
  const [pendingKind, setPendingKind] = useState<JobKind | null>(null);

  function startJob(id: string, kind: JobKind) {
    setPendingKind(kind);
    jobPoll.watch(id);
  }

  // Reopening a past run shows it where runs are shown now: inside New.
  function openRun(id: string) {
    setPendingKind(null);
    setActiveTab("new");
    jobPoll.watch(id);
  }

  const tabs: TabDef[] = [
    { id: "new", label: "New", icon: <Plus size={13} /> },
    // The papers that arrive without being asked for. Sits beside New
    // because the two are the same act -- getting a paper in -- differing
    // only in whether you have to be there when it happens.
    { id: "daily", label: "Daily reader", icon: <Sunrise size={13} /> },
    // Browse leads, because it is the journey most readers want: a date and a
    // subject, not a query. Search is for when you already know the words.
    { id: "browse", label: "Browse", icon: <CalendarDays size={13} /> },
    { id: "categories", label: "Categories", icon: <Tags size={13} /> },
  ];

  const trackedJob = jobPoll.detail?.job ?? null;

  return (
    <div className={styles.page}>
      <Header health={health} checking={checking} unreachable={unreachable} />

      <div className={styles.tabRow}>
        <TabBar tabs={tabs} active={activeTab} onChange={(id) => setActiveTab(id as TabId)} />
      </div>

      <TabPanel active={activeTab === "daily"}>
        <DailyReaderView />
      </TabPanel>

      <TabPanel active={activeTab === "new"}>
        <NewExtractionView
          onSubmitted={(id) => startJob(id, "page")}
          job={pendingKind === "page" ? trackedJob : null}
          polling={pendingKind === "page" && jobPoll.active}
          detail={jobPoll.detail}
          onOpen={openRun}
        />
      </TabPanel>

      <TabPanel active={activeTab === "browse"}>
        <BrowseView active={activeTab === "browse"} />
      </TabPanel>

      <TabPanel active={activeTab === "categories"}>
        <CategoryAdminView active={activeTab === "categories"} />
      </TabPanel>

    </div>
  );
}
