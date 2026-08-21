import {
  Database,
  FileText,
  History as HistoryIcon,
  Loader2,
  Plus,
  Search,
} from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import styles from "./App.module.css";
import { Header } from "./components/Header";
import { TabBar, type TabDef } from "./components/TabBar";
import { useHealth } from "./hooks/useHealth";
import { useJobPoll } from "./hooks/useJobPoll";
import type { JobKind } from "./types";
import { HistoryView } from "./views/HistoryView";
import { NewExtractionView } from "./views/NewExtractionView";
import { ResultView } from "./views/ResultView";
import { SearchView } from "./views/SearchView";
import { TopicSearchView } from "./views/TopicSearchView";

// "archive" is the UI's name for what the backend calls the corpus. The
// distinction is deliberate: /api/corpus, epaper/corpus.py and the
// epaper_corpus database keep the technical term, while the tab says the
// thing a reader would say.
type TabId = "new" | "topic" | "archive" | "result" | "history";

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

  useEffect(() => {
    if (jobPoll.detail?.job.status === "done") {
      setActiveTab("result");
    }
  }, [jobPoll.detail?.job.status]);

  function startJob(id: string, kind: JobKind) {
    setPendingKind(kind);
    jobPoll.watch(id);
  }

  function openFromHistory(id: string) {
    setPendingKind(null);
    setActiveTab("result");
    jobPoll.watch(id);
  }

  const tabs: TabDef[] = [
    { id: "new", label: "New", icon: <Plus size={13} /> },
    { id: "topic", label: "Topic search", icon: <Search size={13} /> },
    { id: "archive", label: "Archive", icon: <Database size={13} /> },
    {
      id: "result",
      label: "Result",
      icon: jobPoll.active ? <Loader2 size={13} className="spin" /> : <FileText size={13} />,
    },
    { id: "history", label: "History", icon: <HistoryIcon size={13} /> },
  ];

  const trackedJob = jobPoll.detail?.job ?? null;

  return (
    <div className={styles.page}>
      <Header health={health} checking={checking} unreachable={unreachable} />

      <div className={styles.tabRow}>
        <TabBar tabs={tabs} active={activeTab} onChange={(id) => setActiveTab(id as TabId)} />
      </div>

      <TabPanel active={activeTab === "new"}>
        <NewExtractionView
          onSubmitted={(id) => startJob(id, "page")}
          job={pendingKind === "page" ? trackedJob : null}
          polling={pendingKind === "page" && jobPoll.active}
        />
      </TabPanel>

      <TabPanel active={activeTab === "topic"}>
        <TopicSearchView
          onSubmitted={(id) => startJob(id, "topic")}
          job={pendingKind === "topic" ? trackedJob : null}
          polling={pendingKind === "topic" && jobPoll.active}
        />
      </TabPanel>

      <TabPanel active={activeTab === "archive"}>
        <SearchView active={activeTab === "archive"} />
      </TabPanel>

      <TabPanel active={activeTab === "result"}>
        <ResultView detail={jobPoll.detail} />
      </TabPanel>

      <TabPanel active={activeTab === "history"}>
        <HistoryView active={activeTab === "history"} onOpen={openFromHistory} />
      </TabPanel>
    </div>
  );
}
