"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSse } from "@/lib/useSse";
import type { ApprovalRow } from "@/lib/types";

interface ApprovalEvent {
  type: "approval";
  approval: ApprovalRow;
}

const STATUS_VARIANT: Record<string, "secondary" | "destructive" | "outline"> = {
  pending: "outline",
  approved: "secondary",
  rejected: "destructive",
  expired: "destructive",
};

const NOT_WIRED_TITLE =
  "Not wired yet — needs API Gateway + Lambda (LLD §11.2), a separate follow-up. " +
  "The dashboard only ever holds a read-only DB credential.";

const getApprovalKey = (e: ApprovalEvent) => e.approval.approval_id;

// Approve/Reject render but are deliberately disabled: LLD §11.2 puts the real mutation behind
// API Gateway + Lambda (`POST /approvals/{id}`), and HLD §5.6 is explicit that no DB write
// credential belongs in the frontend/serverless layer -- engram_reader is SELECT-only, by
// construction (db/migrations/002_grants.sql, 005_reader_approvals_grant.sql). Wiring a real
// mutation here would mean either faking a write path this role can't perform, or quietly
// smuggling in a second, write-capable DSN -- both worse than an honest disabled button.
// Plain `title` attributes, not the shadcn Tooltip component: Base UI's Tooltip.Trigger (this
// shadcn preset's primitive, not Radix) doesn't support `asChild`, and native disabled buttons
// don't reliably fire the hover events a real tooltip needs anyway -- title is simpler and works.
export function ApprovalQueuePanel() {
  const { events, connected } = useSse<ApprovalEvent>("/api/sse/approvals", getApprovalKey);
  const approvals = [...events].map((e) => e.approval).reverse();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          <span>Approval Queue</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500" : "bg-muted-foreground/30"}`}
          />
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-72">
          {approvals.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pending approvals.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {approvals.map((approval) => (
                <li
                  key={approval.approval_id}
                  className="flex flex-col gap-2 rounded-lg border p-2 text-sm"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-muted-foreground">
                      {approval.approval_id.slice(0, 8)}
                    </span>
                    <Badge variant={STATUS_VARIANT[approval.status] ?? "outline"}>
                      {approval.status}
                    </Badge>
                  </div>
                  <div className="flex gap-2" title={NOT_WIRED_TITLE}>
                    <Button size="sm" variant="secondary" disabled className="w-full">
                      Approve
                    </Button>
                    <Button size="sm" variant="destructive" disabled className="w-full">
                      Reject
                    </Button>
                  </div>
                  <div className="text-xs text-muted-foreground">
                    requested {new Date(approval.requested_at).toLocaleTimeString()}
                    {approval.decided_at &&
                      ` · decided ${new Date(approval.decided_at).toLocaleTimeString()} by ${approval.decided_by ?? "?"}`}
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
