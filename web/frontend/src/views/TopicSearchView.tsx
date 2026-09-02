import { AlertTriangle, Check, ChevronDown, Info, Search, Sparkles, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { BeatPicker } from "../components/BeatPicker";
import { CatalogueFields } from "../components/CatalogueFields";
import { bestEdition, catalogueGap, coverageWarning, editionMatch } from "../lib/coverage";
import { ProgressPanel, type ProgressState } from "../components/ProgressPanel";
import { useCatalogue } from "../hooks/useCatalogue";
import type { Proximity, Timeframe } from "../api";
import type { Beat, Expansion, JobRow, TopicMode } from "../types";

import styles from "./TopicSearchView.module.css";

const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;

const TIMEFRAMES: { id: Timeframe; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "3d", label: "Past 3 days" },
  { id: "week", label: "Past week" },
  { id: "month", label: "Past month" },
  { id: "custom", label: "Custom range" },
  { id: "all", label: "Everything" },
];

const PROXIMITIES: { id: Proximity; label: string }[] = [
  { id: "any", label: "Anywhere in the article" },
  { id: "exact", label: "Exact phrase" },
  { id: "w5", label: "Within 5 words" },
  { id: "w10", label: "Within 10 words" },
];

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

  // Refinements. `archive` is the one that changes what the others can do:
  // fetching reads one date's pages, so a range is only meaningful against
  // articles already stored.
  const [archive, setArchive] = useState(false);
  const [timeframe, setTimeframe] = useState<Timeframe>("today");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [days, setDays] = useState<string[]>([]);
  const [exclude, setExclude] = useState("");
  const [proximity, setProximity] = useState<Proximity>("any");
  const [showSuggestions, setShowSuggestions] = useState(true);
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

  // An archive search fetches nothing, so it needs no publisher, edition or
  // date — only a question. Demanding them would be asking the reader to name
  // the page they want before searching for it.
  const canSearch =
    !!topic.trim() &&
    (archive || !!(catalogue.host && catalogue.titleId && catalogue.date)) &&
    !submitting;

  async function search() {
    setSubmitError("");
    setSubmitting(true);
    try {
      const r = await api.topicSearch({
        host: archive ? "" : catalogue.host,
        title_id: archive ? "" : catalogue.titleId,
        date: archive ? "" : catalogue.date,
        topic: topic.trim(),
        edition: archive ? "" : (catalogue.edition?.name ?? ""),
        max_pages: maxPages || 2,
        mode: effectiveMode,
        scope: archive ? "archive" : "fetch",
        timeframe: archive ? timeframe : undefined,
        date_from: archive && timeframe === "custom" ? from : undefined,
        date_to: archive && timeframe === "custom" ? to : undefined,
        weekdays: archive && days.length ? days : undefined,
        exclude: exclude.trim() || undefined,
        proximity: proximity === "any" ? undefined : proximity,
      });
      onSubmitted(r.job_id);
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  }

  function toggleDay(d: string) {
    setDays((cur) => (cur.includes(d) ? cur.filter((x) => x !== d) : [...cur, d]));
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

        {/* Nothing here applies to an archive search: it reads no pages, so
            there is no publisher to choose, no edition to fetch from and no
            page budget to spend. Hidden rather than disabled — a row of dead
            controls is worse than an absent one. */}
        {archive ? (
          <div className={`${styles.coverage} ${styles.coverageInfo}`}>
            <Info size={14} />
            <span>
              Searching everything already extracted — no newspaper, edition or
              page count needed. Untick <strong>Search only my saved archive</strong>{" "}
              below to fetch new pages instead.
            </span>
          </div>
        ) : (
          <>
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

          </>
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

          {/* Collapsible, because nine of these pushed everything below them
              off the screen. They set the topic rather than filtering it, so
              exactly one can be active — which is why this is a disclosure
              with a count and not a Select all / Clear all pair. */}
          <button
            type="button"
            className={styles.disclosure}
            aria-expanded={showSuggestions}
            aria-controls="topic-suggestions"
            onClick={() => setShowSuggestions((v) => !v)}
          >
            <span className="field-label">Suggested topics</span>
            <span className={styles.disclosureMeta}>
              {SUGGESTED_TOPICS.length}
              <ChevronDown
                size={13}
                className={showSuggestions ? styles.caretOpen : styles.caret}
              />
            </span>
          </button>
          {showSuggestions && (
            <div className={styles.chips} id="topic-suggestions">
              {SUGGESTED_TOPICS.map((t) => (
                <button
                  key={t}
                  type="button"
                  className={`${styles.chip} ${topic === t ? styles.chipOn : ""}`}
                  aria-pressed={topic === t}
                  onClick={() => setTopic(topic === t ? "" : t)}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* ------------------------------------------------------ refine */}
        <div className={styles.refine}>
          <span className={styles.stepLabel}>3 · Refine</span>

          <label className={styles.switch}>
            <input
              type="checkbox"
              checked={archive}
              onChange={(e) => setArchive(e.target.checked)}
            />
            <span>
              <strong>Search only my saved archive</strong>
              <span className={styles.switchHint}>
                {archive
                  ? "Answers from pages already read — milliseconds, and can span a date range."
                  : "Fetches and reads the pages for one date. Minutes per page."}
              </span>
            </span>
          </label>

          <div className={styles.refineGrid}>
            <div className={styles.topicField}>
              <label className="field-label" htmlFor="topic-exclude">
                Exclude
              </label>
              <input
                id="topic-exclude"
                type="text"
                placeholder="traffic, parking"
                value={exclude}
                onChange={(e) => setExclude(e.target.value)}
              />
              <p className={styles.fieldHint}>
                Comma separated. An article containing any of these is dropped.
              </p>
            </div>

            <div className={styles.topicField}>
              <label className="field-label" htmlFor="topic-prox">
                Term proximity
              </label>
              <select
                id="topic-prox"
                value={proximity}
                onChange={(e) => setProximity(e.target.value as Proximity)}
              >
                {PROXIMITIES.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.label}
                  </option>
                ))}
              </select>
              <p className={styles.fieldHint}>
                How close the words you typed must sit to each other.
              </p>
            </div>
          </div>

          {/* Only meaningful against stored articles: fetching reads one
              date's pages, so there is no range to range over. Hidden rather
              than disabled — a control that can never apply here is noise. */}
          {archive && (
            <div className={styles.refineGrid}>
              <div className={styles.topicField}>
                <label className="field-label" htmlFor="topic-timeframe">
                  Timeframe
                </label>
                <select
                  id="topic-timeframe"
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value as Timeframe)}
                >
                  {TIMEFRAMES.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.label}
                    </option>
                  ))}
                </select>
              </div>

              {timeframe === "custom" && (
                <div className={styles.refineGrid}>
                  <div className={styles.topicField}>
                    <label className="field-label" htmlFor="topic-from">
                      From
                    </label>
                    <input
                      id="topic-from"
                      type="date"
                      value={from}
                      onChange={(e) => setFrom(e.target.value)}
                    />
                  </div>
                  <div className={styles.topicField}>
                    <label className="field-label" htmlFor="topic-to">
                      To
                    </label>
                    <input
                      id="topic-to"
                      type="date"
                      value={to}
                      onChange={(e) => setTo(e.target.value)}
                    />
                  </div>
                </div>
              )}

              <div className={styles.topicField}>
                <span className="field-label">Days of the week</span>
                <div className={styles.days} role="group" aria-label="Days of the week">
                  {DAYS.map((d) => (
                    <button
                      key={d}
                      type="button"
                      className={`${styles.day} ${days.includes(d) ? styles.dayOn : ""}`}
                      aria-pressed={days.includes(d)}
                      onClick={() => toggleDay(d)}
                    >
                      {d[0].toUpperCase() + d.slice(1)}
                    </button>
                  ))}
                </div>
                <p className={styles.fieldHint}>
                  {days.length === 0
                    ? "Every day. Pick some to compare one weekday across the range."
                    : `Only ${days.length} day${days.length === 1 ? "" : "s"} of the week.`}
                </p>
              </div>
            </div>
          )}
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
