"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
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

// Short, readable titles for the accordion header -- a judge should never have to parse SQL
// just to know what an action WAS. Falls back to a titleized action_kind for anything not
// listed here rather than hardcoding every possible kind.
const ACTION_TITLES: Record<string, string> = {
  create_index: "Index creation proposal",
  drop_index: "Index removal proposal",
  analyze_table: "Table analysis proposal",
};

function actionTitle(actionKind: string): string {
  return ACTION_TITLES[actionKind] ?? `${actionKind.replaceAll("_", " ")} proposal`;
}

const getActionKey = (e: ActionEvent) => e.action.action_id;

export function ActionFeedPanel() {
  const { events, connected, recentKeys } = useSse<ActionEvent>("/api/sse/actions", getActionKey);
  const actions = [...events].reverse();

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-xs tracking-widest text-muted-foreground uppercase">
          <span>Action Feed</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "status-dot-live bg-success" : "bg-muted-foreground/30"}`}
          />
        </CardTitle>
      </CardHeader>
      <CardContent className="flex-1 min-h-0 overflow-hidden">
        <ScrollArea className="h-full">
          {actions.length === 0 ? (
            <p className="text-sm text-muted-foreground">No remediation actions yet.</p>
          ) : (
            // divide-y, not per-row borders -- see TaskFeedPanel's comment on why a boxed `li`
            // inside an already-bordered Card was a real nested-card violation.
            <ul className="flex flex-col divide-y divide-border">
              {actions.map(({ action }) => (
                <ActionFeedRow key={action.action_id} action={action} isNew={recentKeys.has(action.action_id)} />
              ))}
            </ul>
          )}
        </ScrollArea>
      </CardContent>
    </Card>
  );
}

function ActionFeedRow({ action, isNew }: { action: ActionRow; isNew: boolean }) {
  const [open, setOpen] = useState(false);

  return (
    <li
      className={`flex flex-col gap-1 py-2 text-sm ${
        isNew ? "row-enter" : ""
      }`}
    >
      <div className="flex items-center justify-between">
        <span className="font-medium">{action.action_kind}</span>
        <div className="flex gap-1">
          <Badge variant="outline">{action.status}</Badge>
          {action.outcome && (
            <Badge variant={OUTCOME_VARIANT[action.outcome] ?? "outline"}>{action.outcome}</Badge>
          )}
        </div>
      </div>

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 self-start text-xs text-muted-foreground hover:text-foreground"
        aria-expanded={open}
      >
        <ChevronDown className={`size-3 transition-transform ${open ? "rotate-180" : ""}`} />
        {actionTitle(action.action_kind)}
      </button>
      {/* CSS grid-rows trick (globals.css .accordion-rows) -- animates to the SQL block's real
          height with no JS measurement and no fixed max-height guess. */}
      <div className="accordion-rows" data-open={open}>
        <div>
          {/* bg-muted, no border -- a fill-color shift reads as "inset code block" without
              stacking a second box border inside the row's own bottom-divider treatment. */}
          <code className="mt-1 block bg-muted p-2 font-mono text-xs break-all whitespace-pre-wrap text-muted-foreground">
            {action.rendered_sql}
          </code>
        </div>
      </div>

      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>approval: {action.approval_status ?? "—"}</span>
        <span className="font-mono">{new Date(action.created_at).toLocaleTimeString()}</span>
      </div>
    </li>
  );
}
