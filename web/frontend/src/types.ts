export type Platform = "summit" | "readwhere";

export interface Paper {
  name: string;
  host: string;
  lang: string;
  platform: Platform;
}

export interface Edition {
  id: string | number;
  name: string;
}

export interface DateOption {
  date: string;
  issue_id: string | number | null;
}

export interface HealthStatus {
  ready: boolean;
  gpu: string | null;
  layout: boolean;
  mode: string | null;
  vlm: string | null;
  vlm_ok: boolean;
  detail: string;
}

export type JobStatus = "queued" | "running" | "done" | "error";
export type JobKind = "page" | "topic";

export interface JobRow {
  id: string;
  created: string;
  source: string;
  paper: string;
  edition: string;
  date: string;
  page_no: number;
  status: JobStatus;
  stage: string;
  error: string;
  n_articles: number;
  n_ads: number;
  coverage: number;
  width: number;
  height: number;
  with_text: number;
  elapsed: number;
  kind: JobKind;
  topic: string | null;
  searched: number;
  screened: number;
}

export interface ArticleRow {
  job_id: string;
  seq: number;
  type: "article" | "ad" | "masthead" | string;
  headline: string;
  body: string;
  byline: string;
  dateline: string;
  category: string;
  bbox: string;
  crop: string | null;
  n_elements: number;
  has_photo: number;
  topic_score: number | null;
  topic_why: string | null;
  topic_summary: string | null;
  page_ref: number | null;
}

export interface JobDetail {
  job: JobRow;
  articles: ArticleRow[];
}
