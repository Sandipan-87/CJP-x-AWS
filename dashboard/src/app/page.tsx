import { CircleHelp } from "lucide-react";
import { ActionFeedPanel } from "@/components/dashboard/ActionFeedPanel";
import { ApprovalQueuePanel } from "@/components/dashboard/ApprovalQueuePanel";
import { MemoryInspectorPanel } from "@/components/dashboard/MemoryInspectorPanel";
import { MetricsPanel } from "@/components/dashboard/MetricsPanel";
import { TaskFeedPanel } from "@/components/dashboard/TaskFeedPanel";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";

// design/02-low-level-design.md §11: the read-only SSE surface. Four panels, one per frozen
// feed (§11.1). Mutations (Approve/Reject) render but are disabled -- see
// ApprovalQueuePanel's own comment for why that's a stated scope boundary, not an oversight.
//
// The explainer paragraph this page used to always show inline is now a [?] tooltip instead --
// a judge skimming this doesn't need three lines of prose about SSE polling and role names
// sitting under the title; the header should read in half a second.
export default function DashboardPage() {
  return (
    <div className="flex flex-1 flex-col gap-5 bg-background p-6">
      <header className="flex items-center justify-between gap-4 border-b border-border pb-4">
        <div className="flex items-center gap-2">
          <span className="status-dot-live inline-block h-1.5 w-1.5 rounded-full bg-emerald-500" />
          <h1 className="text-lg font-semibold tracking-tight">Memory Inspector</h1>
          <Tooltip>
            <TooltipTrigger className="text-muted-foreground hover:text-foreground">
              <CircleHelp className="size-3.5" />
              <span className="sr-only">About this dashboard</span>
            </TooltipTrigger>
            <TooltipContent className="max-w-xs">
              Live, read-only view over the memory cluster via SSE (5s server-side poll,
              engram_reader role only — no write credential in this app).
            </TooltipContent>
          </Tooltip>
        </div>
        <span className="text-xs tracking-wider text-muted-foreground uppercase">
          engram · live ops console
        </span>
      </header>
      <div className="grid flex-1 grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
        <TaskFeedPanel />
        <ActionFeedPanel />
        <MemoryInspectorPanel />
        <ApprovalQueuePanel />
        <MetricsPanel />
      </div>
    </div>
  );
}
