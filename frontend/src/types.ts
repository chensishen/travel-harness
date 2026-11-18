export type Source = {
  id: string;
  title: string;
  url: string;
  level: string;
  fetched_at: string;
  published_at?: string;
};
export type Block = { text: string; source_ids: string[]; kind?: "fact" | "advice" };
export type Fact = {
  field: string;
  value: string;
  source_ids: string[];
  verified_at: string;
  published_at?: string;
  freshness_note?: string;
};
export type Illustration = {
  local_path: string;
  source_page: string;
  license: string;
  author: string;
  generated?: boolean;
  caption?: string;
};
export type POI = {
  id: string;
  name: string;
  facts: Fact[];
  blocks: Block[];
  images: Illustration[];
  photo_status?: string;
  map_thumbnail?: string;
  hours?: { open: string; close: string; last_entry?: string; closed_days: number[]; note: string };
};
export type Deliverable = {
  spec: TaskSpec;
  prd_version?: string;
  practical?: { category: string; label: string; value: string; source_ids: string[]; estimated: boolean }[];
  reader_checks?: { poi: string; text: string; official_url: string; targets?: { poi: string; url: string }[] }[];
  alternatives?: { name: string; reason: string; replace_day?: string }[];
  pois: POI[];
  days: {
    day: number;
    theme: string;
    stops: POI[];
    notes: string[];
    meals: string[];
    date?: string;
    total_minutes?: number;
    timeline?: { start: string; end: string; place: string; transport: string; note: string }[];
  }[];
  chapters: { title: string; period: string; blocks: Block[]; images: Illustration[] }[];
  sources: Source[];
  warnings: string[];
};
export type JobEvent = {
  seq: number;
  node: string;
  status: string;
  message: string;
  progress: number;
  duration_s?: number;
  source_count?: number;
  at: string;
};
export type TaskProgressData = {
  as_of: string;
  elapsed_s: number;
  elapsed_label: string;
  completed_stages: number;
  total_stages: number;
  remaining_low_s: number | null;
  remaining_high_s: number | null;
  estimated_finish_at: string | null;
  estimate_basis: string;
  estimate_delayed: boolean;
  stages: { id: string; label: string; description: string; status: string; elapsed_s: number | null; message: string; reused: boolean }[];
};
export type TaskSpec = {
  city: string;
  type: string;
  days?: number;
  travelers?: number;
  budget_level?: string;
  season_or_dates?: string;
  start_date?: string | null;
  assumptions: string[];
};
export type GenerationMode = "fast" | "balanced" | "complete";
export type StyleChoice = {
  preset?: string;
  theme?: string;
  template?: string;
  animation?: string;
  prompt?: string;
  template_id?: string;
};

export type Job = {
  id: string;
  prompt: string;
  status: string;
  node: string;
  created_at: string;
  kind: string;
  generation_mode?: GenerationMode;
  style?: StyleChoice;
  error?: string;
  warnings: string[];
  parent_id?: string;
  revision_instruction?: string;
  artifacts: Record<string, string>;
  spec?: TaskSpec;
  options?: {
    days?: number;
    travelers?: number;
    budget_level?: string;
    season_or_dates?: string;
    pace?: string;
    transport?: string;
    dietary?: string;
    focus?: string;
  };
  outline?: { title: string; themes: string[]; poi_names: string[] };
  previews?: string[];
  deliverable?: Deliverable;
  events?: JobEvent[];
  progress?: TaskProgressData;
};
