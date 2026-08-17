import type {
  Beat,
  CorpusStats,
  DateOption,
  Edition,
  Expansion,
  HealthStatus,
  JobDetail,
  JobRow,
  Paper,
  SearchResponse,
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

export interface TopicSearchPayload {
  host: string;
  title_id: string;
  date: string;
  topic: string;
  edition: string;
  max_pages: number;
  min_score?: number;
  mode?: TopicMode;
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
    date_from?: string;
    date_to?: string;
    limit?: number;
  }) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== "" && v !== null) qs.set(k, String(v));
    });
    return req<SearchResponse>(`/api/search?${qs.toString()}`);
  },

  jobs: (limit = 60) => req<JobRow[]>(`/api/jobs?limit=${limit}`),
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

  topicSearch: (payload: TopicSearchPayload) =>
    req<{ job_id: string; platform: string; mode: TopicMode; beat: string | null; n_terms: number }>(
      "/api/jobs/topic",
      json(payload),
    ),

  mediaUrl: (jobId: string, name: string) =>
    `/api/media/${encodeURIComponent(jobId)}/${encodeURIComponent(name)}`,
};
