"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSse } from "@/lib/useSse";
import type { ActionRow } from "@/lib/types";

interface ActionEvent {
  type: "action";
  action: ActionRow;
}

const OUTCOME_VARIANT: Record<string, "secondary" | "destructive" | "outline"> = {
  success: "secondary",
  failure: "destructive",
  noop: "outline",
};

const getActionKey = (e: ActionEvent) => e.action.action_id;

export function ActionFeedPanel() {
  const { events, connected } = useSse<ActionEvent>("/api/sse/actions", getActionKey);
  const actions = [...events].reverse();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          <span>Action Feed</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500" : "bg-muted-foreground/30"}`}
          />
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-72">
          {actions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No remediation actions yet.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {actions.map(({ action }) => (
                <li key={action.action_id} className="flex flex-col gap-1 rounded-lg border p-2 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{action.action_kind}</span>
                    <div className="flex gap-1">
                      <Badge variant="outline">{action.status}</Badge>
                      {action.outcome && (
                        <Badge variant={OUTCOME_VARIANT[action.outcome] ?? "outline"}>
                          {action.outcome}
                        </Badge>
                      )}
                    </div>
                  </div>
                  <code className="truncate text-xs text-muted-foreground">{action.rendered_sql}</code>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>approval: {action.approval_status ?? "—"}</span>
                    <span>{new Date(action.created_at).toLocaleTimeString()}</span>
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
