import type { QueryResultRow } from "pg";
import { getReaderPool } from "@/lib/db";
import type { ActionRow, ApprovalFeedRow, InspectorRow, TaskRow } from "@/lib/types";

// Shared shape for tasks/actions/inspector: all three are "SELECT ... FROM <frozen view>
// ORDER BY created_at" (LLD §11.1). `approvals` is deliberately NOT built on this helper --
// its cursor is "poll status change" against the base table, not a simple created_at feed
// (see fetchApprovalRows below).
function createdAtFeed<T extends QueryResultRow>(query: string) {
  return async (cursor: string | null): Promise<T[]> => {
    const pool = getReaderPool();
    if (cursor) {
      const { rows } = await pool.query<T>(
        `SELECT * FROM (${query}) f WHERE created_at > $1::timestamptz ORDER BY created_at ASC LIMIT 25`,
        [cursor]
      );
      return rows;
    }
    const { rows } = await pool.query<T>(
      `SELECT * FROM (SELECT * FROM (${query}) f ORDER BY created_at DESC LIMIT 25) recent ORDER BY created_at ASC`
    );
    return rows;
  };
}

export const fetchTaskRows = createdAtFeed<TaskRow>(
  `SELECT task_id, task_type, status, trigger, target_cluster_id, created_at, updated_at
   FROM v_recent_tasks`
);

export const fetchActionRows = createdAtFeed<ActionRow>(
  `SELECT action_id, task_id, scope_id, action_kind, status, outcome, rendered_sql,
          created_at, approval_status, decided_by
   FROM v_action_feed`
);

export const fetchInspectorRows = createdAtFeed<InspectorRow>(
  `SELECT item_id, class, content, provenance, created_at, confidence, procedure_status
   FROM v_memory_inspector`
);

// approvals: LLD §11.1 names this feed as reading the `approvals` TABLE directly ("poll status
// change"), not a view -- db/migrations/005_reader_approvals_grant.sql closed the grant gap this
// surfaced. Cursor is COALESCE(decided_at, requested_at): a still-pending row's cursor value is
// its requested_at (so it's only re-sent once, on first connect, unless something changes), and
// once decided, decided_at becomes the sort/cursor key so the resolution itself is pushed too.
export async function fetchApprovalRows(cursor: string | null): Promise<ApprovalFeedRow[]> {
  const pool = getReaderPool();
  const base = `
    SELECT approval_id, task_id, action_id, status, requested_at, decided_at, decided_by,
           channel, comment, COALESCE(decided_at, requested_at) AS cursor_ts
    FROM approvals
  `;
  if (cursor) {
    const { rows } = await pool.query<ApprovalFeedRow>(
      `SELECT * FROM (${base}) f WHERE cursor_ts > $1::timestamptz ORDER BY cursor_ts ASC LIMIT 25`,
      [cursor]
    );
    return rows;
  }
  const { rows } = await pool.query<ApprovalFeedRow>(
    `SELECT * FROM (
       SELECT * FROM (${base}) f ORDER BY cursor_ts DESC LIMIT 25
     ) recent ORDER BY cursor_ts ASC`
  );
  return rows;
}

export function approvalCursor(row: ApprovalFeedRow): string {
  return row.cursor_ts;
}
