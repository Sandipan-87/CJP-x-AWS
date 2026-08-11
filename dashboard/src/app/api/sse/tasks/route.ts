import { fetchTaskRows } from "@/lib/feeds";
import { createSsePollStream, sseHeaders } from "@/lib/sse";

// design/02-low-level-design.md §11.1: `tasks` feed, `v_recent_tasks`, cursor `created_at`.
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function GET() {
  const stream = createSsePollStream(
    fetchTaskRows,
    (row) => row.created_at,
    (row) => ({ type: "task", task: row })
  );
  return new Response(stream, { headers: sseHeaders() });
}
