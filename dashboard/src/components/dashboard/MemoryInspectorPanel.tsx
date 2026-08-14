"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSse } from "@/lib/useSse";
import type { InspectorRow } from "@/lib/types";

interface InspectorEvent {
  type: "recall";
  item: InspectorRow;
}

const getInspectorKey = (e: InspectorEvent) => e.item.item_id;

// This is the "it remembers" demo panel (CLAUDE.md's two demo beats). Confidence + provenance
// are what the frozen v_memory_inspector view actually carries; similarity/citations need
// `decisions.citations` (engram_reader has no grant there yet) -- see the route handler's own
// comment for why that's a stated, separate gap, not silently glossed over here.
export function MemoryInspectorPanel() {
  const { events, connected, recentKeys } = useSse<InspectorEvent>("/api/sse/inspector", getInspectorKey);
  const items = [...events].reverse();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-xs tracking-widest text-muted-foreground uppercase">
          <span>Memory Inspector</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "status-dot-live bg-emerald-500" : "bg-muted-foreground/30"}`}
          />
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-72">
          {items.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No memory items yet — this is what a cold start looks like.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {items.map(({ item }) => (
                <li
                  key={item.item_id}
                  className={`flex flex-col gap-1.5 rounded-lg border border-border p-2.5 text-sm transition-colors ${
                    recentKeys.has(item.item_id) ? "row-enter" : ""
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <Badge variant="outline">{item.class}</Badge>
                    {item.confidence !== null && (
                      <Badge
                        variant="secondary"
                        title={`Wilson lower-bound, time-decayed (invariant #10) — status: ${item.procedure_status ?? "n/a"}`}
                      >
                        confidence {(item.confidence * 100).toFixed(0)}%
                      </Badge>
                    )}
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
