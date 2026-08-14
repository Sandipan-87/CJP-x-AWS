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
// How long a row counts as "just arrived" for the caller's own entrance animation (see
// globals.css's `.row-enter`). Purely a UI decoration signal -- NOT a correctness/dedup
// mechanism, which stays entirely in byKeyRef above.
const RECENT_MS = 900;

export function useSse<T>(path: string, getKey: (event: T) => string, maxEvents = 50) {
  const [events, setEvents] = useState<T[]>([]);
  const [connected, setConnected] = useState(false);
  const [recentKeys, setRecentKeys] = useState<Set<string>>(new Set());
  const byKeyRef = useRef(new Map<string, T>());
  const timersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());

  useEffect(() => {
    const source = new EventSource(path);
    byKeyRef.current = new Map();
    const timers = timersRef.current;

    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false); // EventSource retries on its own
    source.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as T;
        const key = getKey(parsed);
        const byKey = byKeyRef.current;
        const isNewRow = !byKey.has(key);
        byKey.delete(key); // re-insert to move it to the end (most-recent-last)
        byKey.set(key, parsed);
        while (byKey.size > maxEvents) {
          const oldestKey = byKey.keys().next().value;
          if (oldestKey === undefined) break;
          byKey.delete(oldestKey);
        }
        setEvents([...byKey.values()]);

        if (isNewRow) {
          setRecentKeys((prev) => new Set(prev).add(key));
          clearTimeout(timers.get(key));
          timers.set(
            key,
            setTimeout(() => {
              setRecentKeys((prev) => {
                if (!prev.has(key)) return prev;
                const next = new Set(prev);
                next.delete(key);
                return next;
              });
              timers.delete(key);
            }, RECENT_MS)
          );
        }
      } catch {
        // heartbeat comments (": heartbeat ...") never reach onmessage -- SSE comment lines
        // aren't dispatched as events at all, so a parse failure here is a real anomaly, not
        // the expected heartbeat path. Swallow rather than crash the panel either way.
      }
    };

    return () => {
      source.close();
      for (const t of timers.values()) clearTimeout(t);
      timers.clear();
    };
  }, [path, maxEvents, getKey]);

  return { events, connected, recentKeys };
}
