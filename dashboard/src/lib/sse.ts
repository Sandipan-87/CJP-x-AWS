// design/02-low-level-design.md §11.1: "server-side cursor poll, 5s, LIMIT 25, read-only role."
// §7 (CDK/Vercel notes) + §11.1: "route handler maxDuration=60, loop 12x (5s sleep), then
// 200-close; client reconnects." One shared implementation so all four feeds follow the exact
// same timing/cursor contract rather than four slightly-different copies.

export interface SsePollOptions {
  intervalMs?: number; // default 5000 (LLD §11.1)
  iterations?: number; // default 12 (LLD §11.1: 12 * 5s = 60s, matches maxDuration=60)
}

/**
 * `fetchRows(cursor)` must return rows strictly newer than `cursor` (or the initial backlog
 * when `cursor` is null), in ASCENDING order by whatever field `getCursor` reads -- so cursor
 * always advances monotonically and no row is ever delivered twice.
 */
export function createSsePollStream<T>(
  fetchRows: (cursor: string | null) => Promise<T[]>,
  getCursor: (row: T) => string,
  toEvent: (row: T) => unknown,
  opts: SsePollOptions = {}
): ReadableStream<Uint8Array> {
  const intervalMs = opts.intervalMs ?? 5000;
  const iterations = opts.iterations ?? 12;
  const encoder = new TextEncoder();

  // Shared with cancel() below so a client disconnect actually stops the loop early instead of
  // running out its full 12 iterations against a connection nobody is reading anymore.
  const state = { cancelled: false };

  return new ReadableStream<Uint8Array>({
    async start(controller) {
      let cursor: string | null = null;
      for (let i = 0; i < iterations && !state.cancelled; i++) {
        try {
          const rows = await fetchRows(cursor);
          for (const row of rows) {
            const event = toEvent(row);
            controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
            cursor = getCursor(row);
          }
          // heartbeat so the client's connection doesn't look dead during quiet periods
          controller.enqueue(encoder.encode(`: heartbeat ${new Date().toISOString()}\n\n`));
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          controller.enqueue(
            encoder.encode(`event: error\ndata: ${JSON.stringify({ message })}\n\n`)
          );
        }
        if (i < iterations - 1 && !state.cancelled) {
          await new Promise((resolve) => setTimeout(resolve, intervalMs));
        }
      }
      try {
        controller.close();
      } catch {
        // already closed by the client disconnecting -- not an error
      }
    },
    cancel() {
      // client disconnected -- the loop still only checks `cancelled` at iteration boundaries
      // (a mid-sleep disconnect waits out the current setTimeout, up to intervalMs), acceptable
      // given intervalMs=5s and no held per-stream resources (getReaderPool is a shared pool).
      state.cancelled = true;
    },
  });
}

export function sseHeaders(): HeadersInit {
  return {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache, no-transform",
    Connection: "keep-alive",
    "X-Accel-Buffering": "no", // disable nginx-style proxy buffering, if ever fronted by one
  };
}
