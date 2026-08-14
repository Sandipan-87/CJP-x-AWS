"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSse } from "@/lib/useSse";
import type { InspectorRow, RecallCitationRow } from "@/lib/types";

interface InspectorEvent {
  type: "recall";
  item: InspectorRow;
}

interface CitationEvent {
  type: "citation";
  item: RecallCitationRow;
}

const getInspectorKey = (e: InspectorEvent) => e.item.item_id;
const getCitationKey = (e: CitationEvent) => e.item.item_id;

// This is the "it remembers" demo panel (CLAUDE.md's two demo beats). Confidence + provenance
// come from the frozen v_memory_inspector view; similarity comes from a second feed
// (/api/sse/citations, db/migrations/010_reader_recall_citations_grant.sql's
// v_recall_citations), joined onto these rows below by item_id -- each memory item shows its
// own most recent recall similarity score alongside confidence, not a separate panel.
export function MemoryInspectorPanel() {
  const { events, connected, recentKeys } = useSse<InspectorEvent>("/api/sse/inspector", getInspectorKey);
  const { events: citationEvents } = useSse<CitationEvent>("/api/sse/citations", getCitationKey);
  const items = [...events].reverse();
  const similarityByItemId = new Map(citationEvents.map((e) => [e.item.item_id, e.item.similarity]));

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-xs tracking-widest text-muted-foreground uppercase">
          <span>Memory Inspector</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "status-dot-live bg-success" : "bg-muted-foreground/30"}`}
          />
        </CardTitle>
      </CardHeader>
      <CardContent className="flex-1 min-h-0 overflow-hidden">
        <ScrollArea className="h-full">
          {items.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No memory items yet — this is what a cold start looks like.
            </p>
          ) : (
            // divide-y, not per-row borders -- see TaskFeedPanel's comment on the nested-card fix.
            <ul className="flex flex-col divide-y divide-border">
              {items.map(({ item }) => (
                <li
                  key={item.item_id}
                  className={`flex flex-col gap-1 py-2 text-sm ${
                    recentKeys.has(item.item_id) ? "row-enter" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-1">
                    <Badge variant="outline">{item.class}</Badge>
                    <div className="flex items-center gap-1">
                      {similarityByItemId.has(item.item_id) && (
                        <Badge variant="secondary" title="Cosine similarity from the most recent recall citing this memory item">
                          similarity {(similarityByItemId.get(item.item_id)! * 100).toFixed(0)}%
                        </Badge>
                      )}
                      {item.confidence !== null && (
                        <Badge
                          variant="secondary"
                          title={`Wilson lower-bound, time-decayed (invariant #10) — status: ${item.procedure_status ?? "n/a"}`}
                        >
                          confidence {(item.confidence * 100).toFixed(0)}%
                        </Badge>
                      )}
                    </div>
                  </div>
                  <p className="line-clamp-2 text-xs text-muted-foreground">{item.content}</p>
                  <div className="font-mono text-xs text-muted-foreground">
                    {new Date(item.created_at).toLocaleTimeString()}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
