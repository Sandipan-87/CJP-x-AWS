import { fetchInspectorRows } from "@/lib/feeds";
import { createSsePollStream, sseHeaders } from "@/lib/sse";

// design/02-low-level-design.md §11.1: `inspector` feed, `v_memory_inspector`, cursor `created_at`.
// NOTE (stated, not hidden): the frozen event schema here is `{…, confidence, provenance}` --
// it does NOT include per-recall `similarity`/citations (those live in `decisions.citations`,
// which engram_reader has no grant on). §11.3's demo narrative ("Memory Inspector shows...
// similarity, confidence, citations") is broader than what this frozen feed alone delivers;
// closing that gap needs either a grant extension or a second view, deliberately not done in
// this chunk -- scoped out, not silently dropped.
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
