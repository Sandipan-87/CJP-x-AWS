import { fetchActionRows } from "@/lib/feeds";
import { createSsePollStream, sseHeaders } from "@/lib/sse";

// design/02-low-level-design.md §11.1: `actions` feed, `v_action_feed`, cursor `created_at`.
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function GET() {
  const stream = createSsePollStream(
    fetchActionRows,
    (row) => row.created_at,
    (row) => ({ type: "action", action: row })
  );
  return new Response(stream, { headers: sseHeaders() });
}
