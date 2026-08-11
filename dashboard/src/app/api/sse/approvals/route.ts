import { approvalCursor, fetchApprovalRows } from "@/lib/feeds";
import { createSsePollStream, sseHeaders } from "@/lib/sse";

// design/02-low-level-design.md §11.1: `approvals` feed, `approvals` table directly, cursor
// "poll status change" -- db/migrations/005_reader_approvals_grant.sql closed the grant gap
// this surfaced (migration 002 never granted engram_reader SELECT on the base table).
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function GET() {
  const stream = createSsePollStream(
    fetchApprovalRows,
    approvalCursor,
    (row) => ({ type: "approval", approval: row })
  );
  return new Response(stream, { headers: sseHeaders() });
}
