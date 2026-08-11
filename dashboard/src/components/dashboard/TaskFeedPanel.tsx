"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useSse } from "@/lib/useSse";
import type { TaskRow } from "@/lib/types";

interface TaskEvent {
  type: "task";
  task: TaskRow;
}

const STATUS_VARIANT: Record<string, "secondary" | "destructive" | "outline" | "default"> = {
  pending: "outline",
  running: "default",
  awaiting_approval: "secondary",
  blocked: "destructive",
  done: "secondary",
  failed: "destructive",
};

// module-level (stable reference) -- see useSse's own comment on why getKey must not be an
// inline closure recreated every render.
const getTaskKey = (e: TaskEvent) => e.task.task_id;

export function TaskFeedPanel() {
  const { events, connected } = useSse<TaskEvent>("/api/sse/tasks", getTaskKey);
  const tasks = [...events].reverse();

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center justify-between">
          <span>Recent Tasks</span>
          <span
            className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500" : "bg-muted-foreground/30"}`}
            title={connected ? "SSE connected" : "reconnecting…"}
          />
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ScrollArea className="h-72">
          {tasks.length === 0 ? (
            <p className="text-sm text-muted-foreground">No tasks yet — waiting for the first sweep.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {tasks.map(({ task }) => (
                <li key={task.task_id} className="flex flex-col gap-1 rounded-lg border p-2 text-sm">
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-muted-foreground">
                      {task.task_id.slice(0, 8)}
                    </span>
                    <Badge variant={STATUS_VARIANT[task.status] ?? "outline"}>{task.status}</Badge>
                  </div>
                  <div className="flex items-center justify-between text-xs text-muted-foreground">
                    <span>
                      {task.task_type} · {task.trigger}
                    </span>
                    <span>{new Date(task.created_at).toLocaleTimeString()}</span>
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
