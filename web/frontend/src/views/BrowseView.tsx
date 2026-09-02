import { CalendarDays, Loader2, Search } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Timeframe } from "../api";
import { Clipping } from "../components/Clipping";
import { EmptyState } from "../components/EmptyState";
import { CategoryEditor } from "../components/CategoryEditor";
import type {
  ArchiveDate,
  BrowseResponse,
  CategoryRow,
  Facets,
  SearchResponse,
} from "../types";
import styles from "./BrowseView.module.css";

/**
 * Browse the archive by day and subject. The "Browse" tab.
 *
 * The reader path that is not a search. Someone reviewing yesterday has no
 * query -- they have a date, and want to be shown which subjects appeared
 * before choosing one. So the journey is date, then categories with counts, then
 * the clippings, and every step is an indexed read: no scoring, no model, no
 * page fetch.
 *
 * Two ways in, chosen with the Category/Topic switch. Category lists the
 * subjects that appeared and filters to one. Topic takes words and ranks the
 * day's articles against them -- the text search that used to be its own
 * Archive tab, scoped to the day already selected here.
 *
 * Neither fetches a page or calls a model. Both are indexed reads over what
 * has already been extracted, which is what keeps a day's browse instant.
 */
export function BrowseView({ active }: { active: boolean }) {
  const [dates, setDates] = useState<ArchiveDate[]>([]);

  /**
   * What span to read.
   *
   * `range` is a preset resolved on the server; `date` is one specific day
   * chosen from the archive list. They are alternatives, not layers -- a
   * preset clears the day and vice versa -- because a reader who has picked
   * "past 7 days" and then a single date meant the date.
   */
  const [range, setRange] = useState<Timeframe | "">("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [date, setDate] = useState("");
  const [facets, setFacets] = useState<Facets | null>(null);
  const [category, setCategory] = useState("");
  const [tag, setTag] = useState("");
  const [page, setPage] = useState(1);
  const [res, setRes] = useState<BrowseResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [cats, setCats] = useState<CategoryRow[]>([]);

  /**
   * Two ways into the same day. Category is the browse journey -- pick a
   * subject off a list. Topic is the search journey -- type words.
   *
   * Both stay inside the selected day. Browse is date-first, and a topic
   * search that silently answered across the whole archive would be a
   * different feature wearing this one's controls.
   */
  const [mode, setMode] = useState<"category" | "topic">("category");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<SearchResponse | null>(null);
  const [searching, setSearching] = useState(false);

  // Load the day list once the tab is opened, and select the newest day so the
  // view is never an empty screen with a dropdown nobody has touched.
  useEffect(() => {
    if (!active || dates.length) return;
    api
      .archiveDates()
      .then((d) => {
        setDates(d);
        if (d.length && !date) setDate(d[0].date);
      })
      .catch((e) => setErr((e as Error).message));
  }, [active, dates.length, date]);

  // The editor offers existing categories as suggestions. Loaded once per visit,
  // and refreshed after a correction so a newly typed category appears for the
  // next article without a reload.
  const loadCats = useCallback(() => {
    api
      .categories()
      .then((r) => setCats(r.categories || []))
      .catch(() => setCats([]));
  }, []);

  useEffect(() => {
    if (active) loadCats();
  }, [active, loadCats]);

  // Changing the day invalidates the category chosen under the previous one --
  // "crime" on Tuesday is not the same set as "crime" on Monday, and leaving
  // it selected would show a filtered list the reader did not ask for.
  /** The span as the API wants it, whichever control set it. */
  const span = useCallback(
    () =>
      range === "custom"
        ? { date_from: from || undefined, date_to: to || undefined }
        : range
          ? { timeframe: range }
          : { date },
    [range, from, to, date],
  );

  // Changing the span invalidates the category chosen under the previous one
  // -- "crime" this week is not the same set as "crime" on Monday, and
  // leaving it selected would show a filtered list nobody asked for.
  useEffect(() => {
    if (!date && !range) return;
    setCategory("");
    setTag("");
    setPage(1);
    api
      .facets(span())
      .then(setFacets)
      .catch((e) => setErr((e as Error).message));
  }, [date, range, from, to, span]);

  const load = useCallback(async () => {
    if (!date && !range) return;
    setBusy(true);
    setErr("");
    try {
      setRes(
        await api.browse({
          ...span(),
          category: category || undefined,
          tag: tag || undefined,
          page,
          per_page: 25,
        }),
      );
    } catch (e) {
      setErr((e as Error).message);
      setRes(null);
    } finally {
      setBusy(false);
    }
  }, [span, category, tag, page]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  async function runTopicSearch() {
    const query = q.trim();
    if (!query || (!date && !range)) return;
    setSearching(true);
    setErr("");
    try {
      // The same span the category side is reading, so switching between the
      // two modes never silently changes which days are being answered about.
      const sp = span();
      setHits(
        await api.search({
          q: query,
          limit: 100,
          ...("date" in sp ? { date_from: sp.date, date_to: sp.date } : sp),
        }),
      );
    } catch (e) {
      setErr((e as Error).message);
      setHits(null);
    } finally {
      setSearching(false);
    }
  }

  /**
   * The span in words, for the lines that report what was read.
   *
   * Every summary used to end "on {date}", which with a preset selected
   * printed "on " and stopped -- the date is empty precisely when a preset is
   * doing the work.
   */
  const spanLabel = (): string => {
    if (date) return `on ${date}`;
    if (range === "custom") {
      if (from && to) return `between ${from} and ${to}`;
      if (from) return `since ${from}`;
      if (to) return `up to ${to}`;
      return "across the archive";
    }
    return {
      today: "today",
      yesterday: "yesterday",
      "3d": "in the past 3 days",
      week: "in the past 7 days",
      month: "in the past 30 days",
      all: "across the archive",
    }[range as string] ?? "";
  };

  function pickCategory(id: string) {
    setCategory((c) => (c === id ? "" : id));
    setTag("");
    setPage(1);
  }

  function pickTag(t: string) {
    setTag((v) => (v === t ? "" : t));
    setPage(1);
  }

  /**
   * Patch the edited row in place rather than reloading the list. Reloading
   * would move the article: correcting its category can take it out of the
   * filter the reader is looking at, and having a row vanish the moment you
   * fix it reads as having lost it.
   */
  function applyEdit(uid: string, nextCategory: string, nextTags: string[]) {
    setRes((prev) =>
      prev
        ? {
            ...prev,
            results: prev.results.map((r) =>
              r.uid === uid
                ? { ...r, category: nextCategory, tags: nextTags,
                    category_source: "human" }
                : r,
            ),
          }
        : prev,
    );
    // The counts behind the chips have changed, so refresh those.
    api.facets({ date }).then(setFacets).catch(() => undefined);
    loadCats();
  }

  const day = dates.find((d) => d.date === date);
  const behind = day ? day.n - day.labelled : 0;

  return (
    <div className={styles.stack}>
      <div className={styles.spanRow}>
        <div className={styles.presets} role="group" aria-label="Which days">
          {(
            [
              ["today", "Today"],
              ["yesterday", "Yesterday"],
              ["week", "Past 7 days"],
              ["month", "Past 30 days"],
              ["custom", "Custom"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              className={range === id ? styles.presetOn : styles.preset}
              aria-pressed={range === id}
              onClick={() => {
                setRange(range === id ? "" : id);
                setDate("");
              }}
            >
              {label}
            </button>
          ))}
        </div>

        {/* One specific day, from what the archive actually holds. Kept
            alongside the presets because "the paper I read on the 17th" is a
            different question from "this week", and a date picker cannot
            tell you which days have anything in them. */}
        <div className={styles.field}>
          <label htmlFor="browse-date">or one day</label>
          <select
            id="browse-date"
            value={date}
            onChange={(e) => {
              setDate(e.target.value);
              setRange("");
            }}
          >
            <option value="">
              {dates.length ? "pick a day" : "no days in the archive yet"}
            </option>
            {dates.map((d) => (
              <option key={d.date} value={d.date}>
                {d.date} — {d.n} article{d.n === 1 ? "" : "s"}
                {d.labelled < d.n ? ` (${d.n - d.labelled} unlabelled)` : ""}
              </option>
            ))}
          </select>
        </div>
      </div>

      {range === "custom" && (
        <div className={styles.row}>
          <div className={styles.field}>
            <label htmlFor="browse-from">From</label>
            <input
              id="browse-from"
              type="date"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </div>
          <div className={styles.field}>
            <label htmlFor="browse-to">To</label>
            <input
              id="browse-to"
              type="date"
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
          </div>
        </div>
      )}

      <div className={styles.modeRow} role="group" aria-label="Search by">
        <span className={styles.label}>Search by</span>
        <div className={styles.modes}>
          <button
            type="button"
            className={mode === "category" ? styles.modeOn : styles.mode}
            aria-pressed={mode === "category"}
            onClick={() => setMode("category")}
          >
            Category
          </button>
          <button
            type="button"
            className={mode === "topic" ? styles.modeOn : styles.mode}
            aria-pressed={mode === "topic"}
            onClick={() => setMode("topic")}
          >
            Topic
          </button>
        </div>
      </div>

      {mode === "topic" && (
        <div className={styles.searchRow}>
          <input
            type="search"
            value={q}
            placeholder="Words to look for on this day, e.g. police firing"
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void runTopicSearch();
            }}
            aria-label="Topic to search for"
          />
          <button
            className="btn btn-primary"
            onClick={() => void runTopicSearch()}
            disabled={!q.trim() || !date || searching}
          >
            {searching ? <Loader2 size={13} className="spin" /> : <Search size={13} />}
            Search
          </button>
        </div>
      )}

      {err && <p className={styles.err}>{err}</p>}

      {mode === "category" && facets && facets.categories.length > 0 && (
        <div>
          <p className={styles.label}>Categories {spanLabel()}</p>
          <div className={styles.chipRow}>
            {facets.categories.map((c) => (
              <button
                key={c.category}
                type="button"
                className={category === c.category ? styles.chipOn : styles.chip}
                onClick={() => pickCategory(c.category)}
              >
                {c.label}
                <span className={styles.count}>{c.n}</span>
              </button>
            ))}
            {facets.unlabelled > 0 && (
              <span
                className={styles.chipMuted}
                title="Extracted, but the classifier has not run on these yet"
              >
                not yet classified
                <span className={styles.count}>{facets.unlabelled}</span>
              </span>
            )}
          </div>
        </div>
      )}

      {mode === "category" && facets && facets.tags.length > 0 && (
        <div>
          <p className={styles.label}>Narrow by keyword</p>
          <div className={styles.chipRow}>
            {facets.tags.slice(0, 18).map((t) => (
              <button
                key={t.tag}
                type="button"
                className={tag === t.tag ? styles.tagOn : styles.tag}
                onClick={() => pickTag(t.tag)}
              >
                {t.tag}
                <span className={styles.count}> {t.n}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {mode === "topic" && hits && !searching && (
        <>
          <p className={styles.summary}>
            <strong>{hits.total}</strong> article{hits.total === 1 ? "" : "s"} matching
            {" “"}{hits.query}{"”"} {spanLabel()}
            {hits.beat ? ` · ${hits.beat} vocabulary` : ""}
            {" · best match first"}
          </p>
          {hits.results.length === 0 ? (
            <EmptyState
              icon={<Search size={20} />}
              title="Nothing matched"
              sub={`No article ${spanLabel()} matches “${hits.query}”. Try fewer words, or switch to Category.`}
            />
          ) : (
            <div className={styles.results}>
              {hits.results.map((r) => (
                <Clipping key={r.uid} hit={r} score={r.score} />
              ))}
            </div>
          )}
        </>
      )}

      {mode === "category" && busy && (
        <p className={styles.summary}>
          <Loader2 size={13} className="spin" /> loading…
        </p>
      )}

      {mode === "category" && res && !busy && (
        <>
          <p className={styles.summary}>
            <strong>{res.total}</strong> article{res.total === 1 ? "" : "s"}
            {category ? ` filed under “${category}”` : ""}
            {tag ? ` tagged “${tag}”` : ""} {spanLabel()}
            {behind > 0 && !category ? ` · ${behind} still unlabelled` : ""}
            {" · in printed order"}
          </p>

          {res.results.length === 0 ? (
            <EmptyState
              icon={<CalendarDays size={20} />}
              title="Nothing filed here"
              sub={
                category
                  ? `No articles under “${category}” ${spanLabel()}.`
                  : `Nothing found ${spanLabel()}.`
              }
            />
          ) : (
            <div className={styles.results}>
              {res.results.map((r) => (
                <Clipping
                  key={r.uid}
                  hit={r}
                  onTag={pickTag}
                  activeTag={tag}
                >
                  <div className={styles.editRow}>
                    <CategoryEditor
                      uid={r.uid}
                      category={r.category}
                      tags={r.tags}
                      source={r.category_source}
                      options={cats}
                      onSaved={(c, t) => applyEdit(r.uid, c, t)}
                    />
                  </div>
                </Clipping>
              ))}
            </div>
          )}

          {res.pages > 1 && (
            <div className={styles.pager}>
              <button
                type="button"
                disabled={res.page <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                Previous
              </button>
              <span className={styles.pageNo}>
                page {res.page} of {res.pages}
              </span>
              <button
                type="button"
                disabled={res.page >= res.pages}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
