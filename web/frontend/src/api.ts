import type {
  ArchiveDate,
  Beat,
  BrowseResponse,
  CategoryRow,
  CorpusStats,
  DailyReport,
  Facets,
  Headings,
  DateOption,
  Edition,
  Expansion,
  HealthStatus,
  JobDetail,
  JobRow,
  Paper,
  SearchHit,
  ScheduleState,
  SearchResponse,
  Source,
  TopicMode,
} from "./types";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, init);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    let message = text;
    try {
      const parsed = JSON.parse(text);
      message = parsed.detail || parsed.error || text;
    } catch {
      // plain text error body, use as-is
    }
    throw new Error((message || `${res.status} ${res.statusText}`).slice(0, 300));
  }
  return res.json() as Promise<T>;
}

/** Drops empty values, so an unset filter never becomes `?date=` in the URL. */
const qs = (params: Record<string, unknown>) => {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== "" && v !== null) p.set(k, String(v));
  });
  return p.toString();
};

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export interface FetchPagePayload {
  host: string;
  title_id: string;
  date: string;
  page_no: number;
  edition: string;
  with_text: boolean;
}

/** How close the topic's own words must sit to each other in the article. */
export type Proximity = "exact" | "w5" | "w10" | "any";

/** A date span, resolved server-side so it means the same at 23:59 and 00:01. */
export type Timeframe =
  | "today"
  | "yesterday"
  | "3d"
  | "week"
  | "month"
  | "custom"
  | "all";

export interface TopicSearchPayload {
  /** Empty for an archive search, which fetches nothing and needs no title. */
  host: string;
  title_id: string;
  date: string;
  topic: string;
  edition: string;
  max_pages: number;
  min_score?: number;
  mode?: TopicMode;

  /**
   * "archive" answers from what has already been extracted — milliseconds, and
   * able to span a range. Anything else fetches and reads pages for one date.
   */
  scope?: "fetch" | "archive";
  timeframe?: Timeframe;
  date_from?: string;
  date_to?: string;
  /** Day names ("mon".."sun"). Empty and all seven both mean every day. */
  weekdays?: string[];
  /** Comma-separated terms; an article containing any of them is dropped. */
  exclude?: string;
  proximity?: Proximity;
}

export const api = {
  health: () => req<HealthStatus>("/api/health"),
  papers: () => req<Paper[]>("/api/papers"),
  beats: () => req<Beat[]>("/api/beats"),
  expand: (topic: string) => req<Expansion>(`/api/expand?topic=${encodeURIComponent(topic)}`),
  editions: (host: string) => req<Edition[]>(`/api/editions?host=${encodeURIComponent(host)}`),
  dates: (host: string, titleId: string) =>
    req<DateOption[]>(
      `/api/dates?host=${encodeURIComponent(host)}&title_id=${encodeURIComponent(titleId)}`,
    ),

  corpus: () => req<CorpusStats>("/api/corpus"),
  backfill: () =>
    req<{ added: number; updated: number; skipped: number; jobs: number }>(
      "/api/corpus/backfill",
      { method: "POST" },
    ),
  search: (params: {
    q: string;
    paper?: string;
    beat?: string;
    category?: string;
    tag?: string;
    date_from?: string;
    date_to?: string;
    timeframe?: string;
    limit?: number;
  }) => req<SearchResponse>(`/api/search?${qs(params)}`),

  // -- browse: the reader who has a date rather than a question ----------
  archiveDates: () => req<ArchiveDate[]>("/api/corpus/dates"),
  facets: (params: {
    date?: string;
    paper?: string;
    timeframe?: string;
    date_from?: string;
    date_to?: string;
  }) => req<Facets>(`/api/facets?${qs(params)}`),
  browse: (params: {
    date?: string;
    category?: string;
    tag?: string;
    paper?: string;
    page?: number;
    per_page?: number;
    /** Resolved server-side, so a page left open over midnight still means
     *  the "today" the reader is looking at. */
    timeframe?: string;
    date_from?: string;
    date_to?: string;
  }) => req<BrowseResponse>(`/api/browse?${qs(params)}`),
  categories: () =>
    req<{ available: boolean; error?: string; categories: CategoryRow[] }>(
      "/api/categories",
    ),
  /** The fixed scheme: what headings exist and what files under each. */
  headings: () => req<Headings>("/api/categories/headings"),

  createCategory: (label: string) =>
    req<CategoryRow>("/api/categories", json({ label })),
  /** Correct one article's category or tags. Marked human, survives relabels. */
  setArticleCategory: (uid: string, body: { category?: string; tags?: string[] }) =>
    req<{ article: SearchHit }>(`/api/article/${encodeURIComponent(uid)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  mergeCategories: (src: string, dst: string) =>
    req<{ merged: string; into: string; articles_moved: number }>(
      "/api/categories/merge",
      json({ src, dst }),
    ),
  hideCategory: (id: string, hidden: boolean) =>
    req<CategoryRow>(`/api/categories/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden }),
    }),

  /** `kind` narrows to certain job kinds, e.g. "page,upload" for the runs
   *  a person started by hand. Filtered in the query, not after it. */
  jobs: (limit = 60, kind?: string) =>
    req<JobRow[]>(`/api/jobs?${qs({ limit, kind })}`),
  job: (id: string) => req<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`),
  deleteJob: (id: string) =>
    req<{ ok: boolean }>(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }),

  upload: (file: File, withText: boolean) => {
    const fd = new FormData();
    fd.append("file", file);
    fd.append("with_text", withText ? "1" : "0");
    return req<{ job_id: string }>("/api/jobs/upload", { method: "POST", body: fd });
  },

  fetchPage: (payload: FetchPagePayload) =>
    req<{ job_id: string }>("/api/jobs/fetch", json(payload)),

  /**
   * A whole edition, or a span of it. Resumable page by page and skips pages
   * already held, which is what makes re-running a forty-page paper cheap.
   * Omit from_page/to_page for every page the publisher lists.
   */
  ingestEdition: (payload: {
    host: string;
    title_id: string;
    date: string;
    edition?: string;
    from_page?: number;
    to_page?: number;
    force?: boolean;
  }) => req<{ job_id: string }>("/api/jobs/ingest", json(payload)),

  topicSearch: (payload: TopicSearchPayload) =>
    req<{ job_id: string; platform: string; mode: TopicMode; beat: string | null; n_terms: number }>(
      "/api/jobs/topic",
      json(payload),
    ),

  mediaUrl: (jobId: string, name: string) =>
    `/api/media/${encodeURIComponent(jobId)}/${encodeURIComponent(name)}`,

  // -- daily reader -------------------------------------------------------

  daily: (date?: string) =>
    req<DailyReport>(`/api/daily${date ? `?date=${encodeURIComponent(date)}` : ""}`),

  collectNow: (date?: string) =>
    req<{ started: boolean; date: string; due: number; papers?: string[]; detail?: string }>(
      `/api/daily/collect${date ? `?date=${encodeURIComponent(date)}` : ""}`,
      { method: "POST" },
    ),

  setSchedule: (enabled: boolean, at?: string) =>
    req<ScheduleState & { at?: string }>("/api/daily/schedule", json({ enabled, at })),

  sources: () => req<{ available: boolean; error?: string; sources: Source[] }>("/api/sources"),

  addSource: (payload: {
    host: string;
    title_id: string;
    edition?: string;
    lang?: string;
    max_pages?: number;
  }) => req<Source>("/api/sources", json(payload)),

  /** Both fields optional and applied independently, so changing the page
   *  limit cannot accidentally re-enable a title somebody switched off. */
  patchSource: (id: string, body: { enabled?: boolean; max_pages?: number }) =>
    req<Source>(`/api/sources/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  removeSource: (id: string) =>
    req<{ deleted: string }>(`/api/sources/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
