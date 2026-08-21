export type Platform = "summit" | "readwhere";

export interface Paper {
  name: string;
  host: string;
  lang: string;
  platform: Platform;
  /** Beat state codes this title circulates in, e.g. ["PB","HR"]. */
  states?: string[];
  /** A national title, offered for every force regardless of `states`. */
  national?: boolean;
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

/** Relevance engine. "hybrid" = keywords then model; "keywords" = lexical only. */
export type TopicMode = "hybrid" | "keywords";

/** A preset topic with a hand-built keyword vocabulary behind it. */
/**
 * One force. There are 36 of these — every state and union territory — so
 * anything rendering them needs to group and filter rather than list.
 * `sample` is only present when /api/beats is called with samples=true.
 */
export interface Beat {
  id: string;
  label: string;
  topic: string;
  n_terms: number;
  kind: string;
  state: string;
  state_hi: string;
  state_code: string;
  /** "state" or "ut" — what the picker groups on. */
  region_kind: string;
  builtin: boolean;
  enabled: boolean;
  n_places: number;
  /** The force's districts and cities — used to tell if an edition covers it. */
  places: string[];
  sample?: string[];
}

/** One hit from the corpus — already extracted, retrieved without a model. */
export interface SearchHit {
  uid: string;
  job_id: string;
  paper: string;
  edition: string;
  date: string;
  /** The API returns `page_number` here; `page_no` is the SQLite job schema. */
  page_number: number;
  headline: string;
  body: string;
  byline: string;
  lang: string;
  category: string;
  bbox: number[];
  crop: string | null;
  text_source: string;
  /** Blend of lexical and beat scores. */
  score: number;
  lexical: number;
  beat_score: number;
  keywords: string[];
  /** Names a different region and never this beat's own. */
  off_patch: boolean;
}

export interface SearchResponse {
  query: string;
  beat: string | null;
  expanded: boolean;
  terms: number;
  scanned: number;
  total: number;
  results: SearchHit[];
}

export interface CorpusStats {
  available: boolean;
  error?: string;
  articles: number;
  earliest?: string | null;
  latest?: string | null;
  by_paper: { paper: string; n: number }[];
  by_beat: { beat: string; n: number }[];
}

export interface Expansion {
  query: string;
  beat: string | null;
  label: string;
  expanded: boolean;
  n_terms: number;
  terms: string[];
}

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
  /** Engine that actually ran — may differ from the one requested. */
  mode: TopicMode | null;
  beat: string | null;
  /** Non-fatal warning on an otherwise successful job (e.g. model was down). */
  note: string | null;
  /** Articles the keyword layer discarded before any model call. */
  prefiltered: number;
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
  /** JSON array of the beat terms found in this article. */
  keywords: string | null;
}

export interface JobDetail {
  job: JobRow;
  articles: ArticleRow[];
}
