import { fetchRecallCitationRows } from "@/lib/feeds";
import { createSsePollStream, sseHeaders } from "@/lib/sse";

// db/migrations/010_reader_recall_citations_grant.sql: `v_recall_citations`, cursor `created_at`
// (same createdAtFeed()/createSsePollStream() convention as the other four feeds). Closes the
// gap the `inspector` route's own comment used to name: similarity, per memory item, now reaches
// the dashboard -- MemoryInspectorPanel joins these events onto its own inspector rows by
// item_id rather than this being a second visible panel.
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function GET() {
  const stream = createSsePollStream(
    fetchRecallCitationRows,
    (row) => row.created_at,
    (row) => ({ type: "citation", item: row })
  );
  return new Response(stream, { headers: sseHeaders() });
}
