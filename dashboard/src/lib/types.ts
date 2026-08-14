// Mirrors db/migrations/001_engram_schema.sql's view definitions exactly -- these are the
// FROZEN dashboard views (§11, "Read-only dashboard views (frozen surface, §11)"). Don't add
// fields here that the views don't actually select; extend the view (with a stated reason,
// migration bump) instead of quietly widening what the dashboard reads.

export interface TaskRow {
  task_id: string;
  task_type: string;
  status: string;
  trigger: string;
  target_cluster_id: string | null;
  created_at: string; // ISO -- pg Date serialized through JSON.stringify
  updated_at: string;
}

export interface ActionRow {
  action_id: string;
  task_id: string;
  scope_id: string;
  action_kind: string;
  status: string;
  outcome: string | null;
  rendered_sql: string;
  created_at: string;
  approval_status: string | null; // from v_action_feed's LEFT JOIN approvals
  decided_by: string | null;
}

export interface InspectorRow {
  item_id: string;
  class: string;
  content: string;
  provenance: Record<string, unknown>;
  created_at: string;
  confidence: number | null; // NULL when the item isn't linked to a procedure
  procedure_status: string | null;
}

// db/migrations/010_reader_recall_citations_grant.sql's `v_recall_citations` view — one row
// per memory item, the MOST RECENT recall citation's similarity score for that item (a narrow
// unnest of decisions.citations, not the raw decisions table). Closes the "it remembers" demo
// beat's similarity-on-screen gap the inspector feed's own route comment used to name.
export interface RecallCitationRow {
  item_id: string;
  similarity: number;
  source: string;
  scope_id: string;
  created_at: string;
}

export interface ApprovalRow {
  approval_id: string;
  task_id: string;
  action_id: string;
  status: string; // pending|approved|rejected|expired
  requested_at: string;
  decided_at: string | null;
  decided_by: string | null;
  channel: string | null;
  comment: string | null;
}

// What the `approvals` feed's query actually returns -- ApprovalRow plus the computed cursor
// column (COALESCE(decided_at, requested_at)), see src/lib/feeds.ts.
export interface ApprovalFeedRow extends ApprovalRow {
  cursor_ts: string;
}

// Mirrors workers/metrics/handler.py's GET /metrics response exactly -- LLD §12's metric table,
// fetched via CloudWatch GetMetricData, not the memory cluster (no engram_reader involvement
// for this panel at all).
export interface MetricDatapoint {
  timestamp: string; // ISO
  value: number;
}

export interface MetricSeries {
  dimensions: Record<string, string>;
  datapoints: MetricDatapoint[];
}

export interface MetricsResponse {
  window: string;
  generated_at: string;
  cached: boolean;
  metrics: Record<string, MetricSeries[]>;
  omitted: string[];
}
