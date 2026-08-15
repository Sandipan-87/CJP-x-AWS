"use client";

import { useState } from "react";
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

const getApprovalKey = (e: ApprovalEvent) => e.approval.approval_id;

// design/02-low-level-design.md §11.2: POST /approvals/{id} -> API Gateway -> Lambda -> DB. This
// panel calls the LOCAL proxy route (src/app/api/approvals/[approvalId]/route.ts), which holds
// the real API Gateway key server-side -- the browser never sees it and never talks to API
// Gateway directly. If infra/ hasn't been deployed yet, that route returns 503, surfaced here as
// a plain inline error rather than a special case -- the failure mode is the same either way
// from this component's point of view (the request didn't succeed, show why).
export function ApprovalQueuePanel() {
  const { events, connected, recentKeys } = useSse<ApprovalEvent>("/api/sse/approvals", getApprovalKey);
  const approvals = [...events].map((e) => e.approval).reverse();

  const [busyId, setBusyId] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});

  async function decide(approvalId: string, decision: "approve" | "reject") {
    setBusyId(approvalId);
    setErrors((prev) => ({ ...prev, [approvalId]: "" }));
    try {
      const res = await fetch(`/api/approvals/${approvalId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, by: "dashboard-user" }),
      });
      if (!res.ok) {
        const payload = await res.json().catch(() => ({}));
        throw new Error(payload.error || `HTTP ${res.status}`);
      }
      // No optimistic update needed -- the approvals SSE feed pushes the resulting status
      // change back within one poll cycle (LLD §11.3: "SSE pushes the state change back to
      // every viewer").
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setErrors((prev) => ({ ...prev, [approvalId]: message }));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <Card className="h-full">
      <CardHeader>
        <CardTitle className="flex items-center justify-between text-xs tracking-widest text-muted-foreground uppercase">
          <span>Approval Queue</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "status-dot-live bg-success" : "bg-muted-foreground/30"}`}
          />
        </CardTitle>
      </CardHeader>
      <CardContent className="flex-1 min-h-0 overflow-hidden">
        <ScrollArea className="h-full">
          {approvals.length === 0 ? (
            <p className="text-sm text-muted-foreground">No pending approvals.</p>
          ) : (
            // divide-y, not per-row borders -- see TaskFeedPanel's comment on the nested-card
            // fix. The old isPending-vs-not border-color distinction is dropped too: the
            // status Badge already carries that signal, so a second border treatment for the
            // same fact was redundant noise, not a second signal.
            <ul className="flex flex-col divide-y divide-border">
              {approvals.map((approval) => {
                const isPending = approval.status === "pending";
                const isBusy = busyId === approval.approval_id;
                const error = errors[approval.approval_id];
                return (
                  <li
                    key={approval.approval_id}
                    className={`flex flex-col gap-1.5 py-2 text-sm ${
                      recentKeys.has(approval.approval_id) ? "row-enter" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-mono text-xs text-muted-foreground">
                        {approval.approval_id.slice(0, 8)}
                      </span>
                      <Badge variant={STATUS_VARIANT[approval.status] ?? "outline"}>
                        {approval.status}
                      </Badge>
                    </div>
                    {/* Buttons render ONLY for a decision a human can actually make. A decided
                        approval showing a disabled Approve/Reject pair was dead chrome nobody
                        could ever click, and disabled:opacity-50 made the brutalist white/red
                        fills read as washed-out grey boxes -- distilled away rather than
                        patched, so every button this panel ever shows is at full, real
                        contrast. */}
                    {isPending && (
                      // grid, not flex -- Button's own base class sets shrink-0, so two
                      // `w-full` flex children each demand 100% width and overflow the row
                      // (silently clipped by Card's overflow-hidden, a real pre-existing bug
                      // found while visually checking the original redesign: Reject rendered
                      // entirely off-screen). A 2-column grid divides the row exactly in half
                      // regardless of shrink.
                      <div className="grid grid-cols-2 gap-2">
                        {/* DESIGN.md §6: emerald-400/--success is this app's one accent
                            color, reserved for "approve" -- variant="success", not the
                            brutalist white/black default. Hover is a flat opacity drop,
                            never a glow. */}
                        <Button
                          size="sm"
                          variant="success"
                          disabled={isBusy}
                          className="w-full"
                          onClick={() => decide(approval.approval_id, "approve")}
                        >
                          {isBusy ? "…" : "Approve"}
                        </Button>
                        <Button
                          size="sm"
                          variant="destructive"
                          disabled={isBusy}
                          className="w-full"
                          onClick={() => decide(approval.approval_id, "reject")}
                        >
                          {isBusy ? "…" : "Reject"}
                        </Button>
                      </div>
                    )}
                    {error && <p className="text-xs text-red-400">{error}</p>}
                    <div className="font-mono text-xs text-muted-foreground">
                      requested {new Date(approval.requested_at).toLocaleTimeString()}
                      {approval.decided_at &&
                        ` · decided ${new Date(approval.decided_at).toLocaleTimeString()} by ${approval.decided_by ?? "?"}`}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
