import { AlertTriangle, Check, Info, Search, Sparkles, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { BeatPicker } from "../components/BeatPicker";
import { CatalogueFields } from "../components/CatalogueFields";
import { bestEdition, catalogueGap, coverageWarning, editionMatch } from "../lib/coverage";
import { ProgressPanel, type ProgressState } from "../components/ProgressPanel";
import { useCatalogue } from "../hooks/useCatalogue";
import type { Beat, Expansion, JobRow, TopicMode } from "../types";
import styles from "./TopicSearchView.module.css";

const SUGGESTED_TOPICS = [
  "courts and judgments",
  "politics and government",
  "civic issues and infrastructure",
  "health and hospitals",
  "education and schools",
  "sports",
  "business and economy",
  "accidents and disasters",
  "agriculture and farmers",
];

export function TopicSearchView({
  onSubmitted,
  job,
  polling,
}: {
  onSubmitted: (jobId: string) => void;
  job: JobRow | null;
  polling: boolean;
}) {
  const catalogue = useCatalogue();
  const [topic, setTopic] = useState("");
  const [maxPages, setMaxPages] = useState(2);
  const [mode, setMode] = useState<TopicMode>("hybrid");
  const [beats, setBeats] = useState<Beat[]>([]);
  const [expansion, setExpansion] = useState<Expansion | null>(null);
  const [showTerms, setShowTerms] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [autoPicked, setAutoPicked] = useState("");

  useEffect(() => {
    api.beats().then(setBeats).catch(() => setBeats([]));
  }, []);

  // Show the vocabulary a topic resolves to before any GPU time is spent on it.
  // Debounced: this fires on every keystroke otherwise.
  useEffect(() => {
    const q = topic.trim();
    if (!q) {
      setExpansion(null);
      return;
    }
    let cancelled = false;
    const t = setTimeout(() => {
      api
        .expand(q)
        .then((e) => !cancelled && setExpansion(e))
        .catch(() => !cancelled && setExpansion(null));
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [topic]);

  // The force the current topic resolves to, if any. Derived from the topic
  // rather than held as its own state: the two would otherwise be able to
  // disagree, and the picker would keep showing a force after the topic had
  // been edited into something else entirely.
  const activeBeat = beats.find((b) => b.id === expansion?.beat) ?? null;
  const activePaper = catalogue.papers.find((p) => p.host === catalogue.host);
  const activeEdition = catalogue.editions.find(
    (e) => String(e.id) === String(catalogue.titleId),
  );
  const warning = coverageWarning(
    activeBeat,
    activePaper,
    activeEdition,
    catalogue.editions,
    catalogue.papers,
  );
  const gap = catalogueGap(catalogue.papers, activeBeat);
  // Only promise ordering that is actually happening. The header used to say
  // "editions covering X are listed first" unconditionally, which read as a
  // straightforward lie against a paper that has no X edition at all.
  const coveringEditions = catalogue.editions.filter((e) =>
    editionMatch(e, activeBeat),
  ).length;

  // Pick the matching edition, rather than merely sorting it to the top.
  //
  // Grouping alone left the force and the edition looking like two unrelated
  // controls: choosing Himachal Pradesh Police reordered a list and then
  // waited, which is not what "these are linked" looks like. Selecting
  // Himachal Edition is.
  //
  // Deliberately narrow. It fires only when the beat changes or a new
  // edition list arrives, and only when the current selection does not
  // already cover the beat — so a deliberate choice of a non-matching edition
  // is never yanked away afterwards. `titleId` is not a dependency, which is
  // what keeps a manual override sticking.
  const beatId = activeBeat?.id ?? "";
  useEffect(() => {
    if (!activeBeat || catalogue.editionsLoading || !catalogue.editions.length) return;
    const current = catalogue.editions.find(
      (e) => String(e.id) === String(catalogue.titleId),
    );
    if (current && editionMatch(current, activeBeat)) return;
    const best = bestEdition(catalogue.editions, activeBeat);
    if (!best || String(best.id) === String(catalogue.titleId)) return;
    catalogue.setTitleId(String(best.id));
    setAutoPicked(best.name);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [beatId, catalogue.editions, catalogue.editionsLoading]);

  // Shown only while the auto-picked edition is still the selected one, so the
  // note disappears the moment it is overridden without needing to be told.
  const autoNote =
    autoPicked && activeEdition?.name === autoPicked ? autoPicked : "";

  // Lexical ranking needs a vocabulary. Without one there is nothing to match
  // beyond the words typed, so the option is withdrawn rather than left to fail.
  const canUseKeywords = !!expansion?.expanded;
  const effectiveMode: TopicMode = mode === "keywords" && !canUseKeywords ? "hybrid" : mode;

  const canSearch =
    !!(catalogue.host && catalogue.titleId && catalogue.date && topic.trim()) && !submitting;

  async function search() {
    setSubmitError("");
    setSubmitting(true);
    try {
      const r = await api.topicSearch({
        host: catalogue.host,
        title_id: catalogue.titleId,
        date: catalogue.date,
        topic: topic.trim(),
        edition: catalogue.edition?.name ?? "",
        max_pages: maxPages || 2,
        mode: effectiveMode,
      });
      onSubmitted(r.job_id);
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  const note = !catalogue.host
    ? ""
    : effectiveMode === "keywords"
      ? catalogue.paper?.platform === "summit"
        ? "Keyword ranking over the publisher's own article text — seconds, and no model involved at all."
        : "Page images still need the layout model to find the articles; only the relevance stages are skipped."
      : catalogue.paper?.platform === "summit"
        ? "Uses the publisher's own article text when that date has it (seconds, exact wording). Otherwise falls back to reading page images — a few minutes per page."
        : "Page images — every article must be found and read first, so allow a few minutes per page.";

  const progressState: ProgressState | null = submitError
    ? "error"
    : job?.status === "done"
      ? "done"
      : job?.status === "error"
        ? "error"
        : job || polling
          ? job?.status === "queued"
            ? "queued"
            : "running"
          : null;

  const doneMessage = job
    ? [
        `found ${job.n_articles} of ${job.searched} in ${(job.elapsed || 0).toFixed(0)}s`,
        job.prefiltered ? `${job.prefiltered} ruled out by keywords` : "",
        job.note || "",
      ]
        .filter(Boolean)
        .join(" · ")
    : "";

  const progressMessage = submitError
    ? submitError
    : job?.status === "done"
      ? doneMessage
      : job?.status === "error"
        ? `failed: ${job.error || "unknown error"}`
        : undefined;

  return (
    <div className={styles.stack}>
      <div className="card" style={{ padding: 22 }}>
        {/* What to look for comes before where to look for it. Picking the
            force first is what lets the newspaper and edition lists group
            themselves around it, and it matches the order of the question
            people actually arrive with. */}
        <div className={styles.searchFor}>
          <span className={styles.stepLabel}>1 · What to look for</span>
          <div className={styles.searchForRow}>
            {beats.length > 0 && (
              <BeatPicker
                id="topic-beat"
                beats={beats}
                value={activeBeat?.id ?? ""}
                onChange={(id) => {
                  const b = beats.find((x) => x.id === id);
                  setTopic(b ? b.label : "");
                }}
                label="Police force"
                anyLabel="Something else…"
                hint={`${beats.length} forces`}
              />
            )}
            <div className={styles.topicField}>
              <label className="field-label" htmlFor="topic-input">
                {activeBeat ? "Topic (from the force above — edit freely)" : "Topic"}
              </label>
              <input
                id="topic-input"
                type="text"
                placeholder="e.g. Delhi Police, or courts and judgments"
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
              />
            </div>
          </div>
        </div>

        <div className={styles.whereRow}>
          <span className={styles.stepLabel}>
            2 · Where to look
            {activeBeat && coveringEditions > 0
              ? ` — ${coveringEditions} edition${coveringEditions === 1 ? "" : "s"} cover ${activeBeat.state}, listed first`
              : ""}
          </span>
        </div>
        <CatalogueFields
          catalogue={catalogue}
          beat={activeBeat}
          trailing={
            <div className={styles.pagesField}>
              <label className="field-label" htmlFor="topic-pages">
                Pages
              </label>
              <input
                id="topic-pages"
                type="number"
                min={1}
                max={60}
                value={maxPages}
                onChange={(e) => setMaxPages(Number(e.target.value) || 1)}
              />
            </div>
          }
        />

        {gap ? (
          <div className={`${styles.coverage} ${styles.coverageInfo}`}>
            <Info size={14} />
            <span>{gap}</span>
          </div>
        ) : warning.level !== "none" ? (
          <div
            className={`${styles.coverage} ${
              warning.level === "paper"
                ? styles.coverageBad
                : warning.level === "no-edition"
                  ? styles.coverageInfo
                  : styles.coverageWarn
            }`}
          >
            {warning.level === "no-edition" ? (
              <Info size={14} />
            ) : (
              <AlertTriangle size={14} />
            )}
            <span>
              <strong>{warning.message}</strong> {warning.suggestion}
            </span>
            {warning.switchTo && (
              <button
                type="button"
                className={styles.switchBtn}
                onClick={() => catalogue.setHost(warning.switchTo!.host)}
              >
                Use {warning.switchTo.name}
              </button>
            )}
          </div>
        ) : (
          autoNote && (
            <div className={`${styles.coverage} ${styles.coverageOk}`}>
              <Check size={14} />
              <span>
                Edition set to <strong>{autoNote}</strong> to match{" "}
                {activeBeat?.state}. Change it above if you meant another.
              </span>
            </div>
          )
        )}

        <div style={{ marginTop: 16 }} className={styles.topicField}>
          {expansion && (
            <div className={styles.expansion}>
              {expansion.expanded ? (
                <>
                  <Sparkles size={12} className={styles.expandIcon} />
                  <span>
                    <strong>{expansion.label}</strong> vocabulary — searching {expansion.n_terms}{" "}
                    related terms in English and Hindi
                  </span>
                  <button
                    type="button"
                    className={styles.linkBtn}
                    onClick={() => setShowTerms((v) => !v)}
                  >
                    {showTerms ? "hide" : "show"}
                  </button>
                </>
              ) : (
                <span className={styles.expandPlain}>
                  No preset vocabulary for this topic — the model reads it as written, with no
                  keyword expansion.
                </span>
              )}
            </div>
          )}

          {showTerms && expansion?.expanded && (
            <div className={styles.termBox}>
              {expansion.terms.map((t) => (
                <span key={t} className={styles.term}>
                  {t}
                </span>
              ))}
            </div>
          )}

          <div className={styles.chips}>
            {SUGGESTED_TOPICS.map((t) => (
              <button key={t} type="button" className={styles.chip} onClick={() => setTopic(t)}>
                {t}
              </button>
            ))}
          </div>
        </div>

        <div className={styles.modeRow}>
          <span className="field-label">Engine</span>
          <div className={styles.modeList}>
            <button
              type="button"
              className={`${styles.mode} ${effectiveMode === "hybrid" ? styles.modeOn : ""}`}
              onClick={() => setMode("hybrid")}
            >
              <Sparkles size={13} /> Keywords + model
              <span className={styles.modeHint}>precise, minutes</span>
            </button>
            <button
              type="button"
              className={`${styles.mode} ${effectiveMode === "keywords" ? styles.modeOn : ""}`}
              onClick={() => setMode("keywords")}
              disabled={!canUseKeywords}
              title={
                canUseKeywords
                  ? "Rank by keyword match only. No relevance model call."
                  : "Needs a preset beat vocabulary — pick a beat above."
              }
            >
              <Zap size={13} /> Keywords only
              <span className={styles.modeHint}>fast, recall-first</span>
            </button>
          </div>
        </div>

        <div className={styles.actions}>
          <p className={styles.note}>{note}</p>
          <button className="btn btn-primary" disabled={!canSearch} onClick={search}>
            <Search size={13} /> Search
          </button>
        </div>
      </div>

      {progressState && (
        <ProgressPanel state={progressState} stage={job?.stage} message={progressMessage} />
      )}
    </div>
  );
}
