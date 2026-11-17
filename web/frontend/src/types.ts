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
  layout_ok?: boolean;
  mode: string | null;
  vlm: string | null;
  vlm_ok: boolean;
  ocr_ok?: boolean;
  ocr?: string | null;
  ocr_error?: string;
  detail: string;
}

export type JobStatus = "queued" | "running" | "done" | "error";
export type JobKind = "page" | "upload" | "edition" | "topic";

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
  /** What the story is ABOUT. Empty until the classifier has labelled it. */
  category: string;
  /** The specific kind within that heading — "murder" under crime. Returned
   *  by every hit builder; was missing here, so nothing rendered it. */
  subcategory: string;
  /** Where it BELONGS — the beat that claims it most strongly. */
  primary_beat: string;
  /** Specifics under the topic — the offence, the place, the organisation. */
  tags: string[];
  /**
   * "human" when a person set the topic by hand. Bulk relabelling skips
   * those, so a correction is not undone by the next overnight run.
   */
  category_source?: string;
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
  /** Articles per beat — geography and desk. */
  by_beat: { beat: string; n: number }[];
  /** Articles per topic. Everything sits under "(unlabelled)" until the
   *  classifier runs. */
  by_category: { category: string; n: number }[];
}

/** One day the archive holds. `labelled` < `n` means classification is behind. */
export interface ArchiveDate {
  date: string;
  n: number;
  labelled: number;
}

/** What appeared on a day — the screen between picking a date and the clips. */
export interface Facets {
  date: string | null;
  paper: string | null;
  total: number;
  /**
   * Articles nobody has classified yet. Distinct from the "unclassified"
   * category, which means the classifier ran and could not label them —
   * a backlog and a quality problem are not the same thing.
   */
  unlabelled: number;
  categories: { category: string; label: string; n: number }[];
  tags: { tag: string; n: number }[];
}

/** Browse results — printed order, not ranked. Scores are always zero. */
export interface BrowseResponse {
  date: string | null;
  category: string | null;
  tag: string | null;
  paper: string | null;
  total: number;
  page: number;
  per_page: number;
  pages: number;
  results: SearchHit[];
}

/** A row in the topic registry, with its merge and visibility state. */
export interface CategoryRow {
  id: string;
  label: string;
  n: number;
  /** Non-null when this entry has been merged away into another. */
  merged_into: string | null;
  hidden: boolean;
  sample_raw: string;
  /** "human" for a topic somebody added before the classifier produced it. */
  created_by?: string;
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

// -- daily collection -------------------------------------------------------

/** A newspaper registered for unattended nightly collection. */
export interface Source {
  id: string;
  host: string;
  title_id: string;
  paper: string;
  edition: string;
  lang: string;
  /** 0 means the whole edition. Most publishers serve only page 1 free. */
  max_pages: number;
  enabled: boolean;
  /** The last date collected, not a timestamp — that is what makes a repeated
   *  run idempotent and a missed night recoverable. */
  last_date: string;
  last_error: string;
  fail_count: number;
}

/**
 * Whether the nightly run is registered with Windows Task Scheduler.
 * Read from the OS rather than from a setting of our own, so it cannot claim
 * "on" for a task that was deleted or never created on this machine.
 */
export interface ScheduleState {
  supported: boolean;
  registered: boolean;
  status?: string;
  next_run?: string;
  last_run?: string;
  last_result?: string;
  detail?: string;
}

/**
 * One paper's contribution to a day.
 *
 * "waiting" is a registered paper whose edition has not arrived yet, which
 * before the morning run is normal rather than a failure.
 */
export interface DailySource {
  source_id: string;
  paper: string;
  edition: string;
  lang: string;
  /** false for a one-off extraction the scheduler knows nothing about. */
  registered: boolean;
  n: number;
  labelled: number;
  pages: number[];
  categories: string[];
  status: "collected" | "waiting" | "failed";
  last_date: string;
  last_error: string;
  fail_count: number;
}

export interface DailyReport {
  date: string;
  total: number;
  collected: number;
  waiting: number;
  failed: number;
  sources: DailySource[];
  schedule: ScheduleState;
}

/** One of the fixed top-level headings, with what belongs under it. */
export interface Heading {
  id: string;
  label: string;
  note: string;
  /** True for the five police-desk headings. */
  desk: boolean;
  /** Seeded subcategories only — not what the classifier has invented. */
  children: string[];
}

export interface Headings {
  headings: Heading[];
  police_desk: string[];
  unclassified: string;
  language_note: string;
}
