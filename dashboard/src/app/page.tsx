import { ActionFeedPanel } from "@/components/dashboard/ActionFeedPanel";
import { ApprovalQueuePanel } from "@/components/dashboard/ApprovalQueuePanel";
import { MemoryInspectorPanel } from "@/components/dashboard/MemoryInspectorPanel";
import { MetricsPanel } from "@/components/dashboard/MetricsPanel";
import { TaskFeedPanel } from "@/components/dashboard/TaskFeedPanel";

// design/02-low-level-design.md §11: the read-only SSE surface. Four panels, one per frozen
// feed (§11.1). Mutations (Approve/Reject) render but are disabled -- see
// ApprovalQueuePanel's own comment for why that's a stated scope boundary, not an oversight.
export default function DashboardPage() {
  return (
    <div className="flex flex-1 flex-col gap-6 bg-zinc-50 p-6 dark:bg-black">
      <header>
        <h1 className="text-xl font-semibold">Engram — Memory Inspector</h1>
        <p className="text-sm text-muted-foreground">
          Live, read-only view over the memory cluster via SSE (5s server-side poll,
          engram_reader role only — no write credential in this app).
        </p>
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
