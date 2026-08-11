"use client";

import { useEffect, useRef, useState } from "react";

// design/02-low-level-design.md §11.1: the server-side route closes after ~60s (maxDuration=60,
// 12 * 5s polls) -- "client reconnects" is part of the frozen contract, not an afterthought.
// Native EventSource already auto-reconnects on a clean server close, but every reconnect starts
// a FRESH server-side poll with cursor=null, which re-sends the same recent backlog rows again --
// confirmed live (seeded one demo task, watched it render twice in the Task Feed panel before
// this dedup existed). `getKey` lets each caller dedupe by whatever ID field its row actually has
// (task_id, action_id, item_id, approval_id) while still keeping "last write wins" semantics for
// rows whose fields change over time (e.g. an approval moving pending -> approved).
export function useSse<T>(path: string, getKey: (event: T) => string, maxEvents = 50) {
  const [events, setEvents] = useState<T[]>([]);
  const [connected, setConnected] = useState(false);
  const byKeyRef = useRef(new Map<string, T>());

  useEffect(() => {
    const source = new EventSource(path);
    byKeyRef.current = new Map();

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false); // EventSource retries on its own
    source.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as T;
        const byKey = byKeyRef.current;
        byKey.delete(getKey(parsed)); // re-insert to move it to the end (most-recent-last)
        byKey.set(getKey(parsed), parsed);
        while (byKey.size > maxEvents) {
          const oldestKey = byKey.keys().next().value;
          if (oldestKey === undefined) break;
          byKey.delete(oldestKey);
        }
        setEvents([...byKey.values()]);
      } catch {
        // heartbeat comments (": heartbeat ...") never reach onmessage -- SSE comment lines
        // aren't dispatched as events at all, so a parse failure here is a real anomaly, not
        // the expected heartbeat path. Swallow rather than crash the panel either way.
      }
    };

    return () => {
      source.close();
    };
  }, [path, maxEvents, getKey]);

  return { events, connected };
}
