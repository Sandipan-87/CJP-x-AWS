import { fetchInspectorRows } from "@/lib/feeds";
import { createSsePollStream, sseHeaders } from "@/lib/sse";

// design/02-low-level-design.md §11.1: `inspector` feed, `v_memory_inspector`, cursor `created_at`.
// The frozen event schema here stays `{…, confidence, provenance}` -- similarity now reaches the
// dashboard via a SEPARATE feed (`/api/sse/citations`, db/migrations/010_reader_recall_citations_
// grant.sql's `v_recall_citations`), joined onto these rows client-side in MemoryInspectorPanel
// by item_id, rather than widening this frozen view/route.
export const dynamic = "force-dynamic";
export const maxDuration = 60;

export async function GET() {
  const stream = createSsePollStream(
    fetchInspectorRows,
    (row) => row.created_at,
    (row) => ({ type: "recall", item: row })
  );
  return new Response(stream, { headers: sseHeaders() });
}
