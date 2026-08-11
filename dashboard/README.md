# Engram Dashboard — read-only SSE surface

[ILLUSIONIST]. `design/02-low-level-design.md` §11 (frozen contract) / HLD §5.6.

**Scope of this chunk, stated explicitly:** this app implements the four read-only SSE feeds
(§11.1: tasks, actions, inspector, approvals) and a UI to view them. It does **not** implement
the mutation path (`POST /approvals/{id}`, §11.2) — that goes through API Gateway + Lambda in
the real architecture specifically so no DB write credential ever needs to sit in a browser or
serverless function. Approve/Reject buttons render, disabled, with a `title` explaining this.

## Setup

1. `db/migrations/002_grants.sql` + `005_reader_approvals_grant.sql` must be applied (they are,
   on the live memory cluster, as of 2026-08-11).
2. `scripts/bootstrap_reader_role.py` (repo root) provisions `ENGRAM_READER_DSN` into the
   repo-root `.env`. Copy that one value into `dashboard/.env.local` (gitignored, never commit):
   ```
   ENGRAM_READER_DSN=postgresql://engram_reader:<pw>@<host>:26257/defaultdb?sslmode=verify-full
   ```
3. Fetch the memory cluster's CA cert (same one `scripts/run_sql.py --sslrootcert` uses) into
   `dashboard/certs/memory-ca.crt` (gitignored):
   ```
   curl -fsSL https://cockroachlabs.cloud/clusters/<ENGRAM_MEMORY_CLUSTER_ID>/cert -o dashboard/certs/memory-ca.crt
   ```
   Without this file, `src/lib/db.ts` falls back to `rejectUnauthorized: false` with a loud
   console warning — fine for a quick local check, never for anything real.
4. `npm install && npm run dev` — http://localhost:3000.

## Why `pg`, not `psycopg`/the Python DAO

This is the only Node.js component in the repo. `agent/memory/db.py`'s DAO is Python-only and
not reachable from a Vercel serverless function; `pg` speaks CockroachDB's Postgres wire
protocol directly, same approach `psycopg` takes on the Python side. No code is shared between
the two — the SQL itself (reading the same frozen views) is the only contract between them.

## Architecture notes carried over from the design docs

- `engram_reader` is SELECT-only: `v_recent_tasks`, `v_action_feed`, `v_memory_inspector`,
  `observations`, `approvals`. No INSERT/UPDATE/DELETE grant exists for this role — verified
  live by `scripts/bootstrap_reader_role.py`, not just declared.
- The `inspector` feed's event schema is `{…, confidence, provenance}` only — no
  `similarity`/citations (those live in `decisions.citations`, ungranted). §11.3's demo
  narrative wants richer detail than this frozen feed alone provides; closing that gap is
  follow-up work (either a grant extension or a second view), not done here.
- SSE routes close after ~60s (`maxDuration=60`, 12×5s polls) by design — the client
  (`src/lib/useSse.ts`) relies on `EventSource`'s native auto-reconnect, matching §11.1's
  "client reconnects" note.
- Every reconnect re-runs the server-side poll from `cursor=null`, which re-sends the recent
  backlog — `useSse` dedupes client-side by a caller-supplied `getKey` (must be a stable,
  module-level function reference, not an inline closure, or the effect re-runs every render
  and reopens the connection). Caught live: seeding one demo task rendered twice in the Task
  Feed panel before this dedup existed.
- **Observed in `next dev` only, not in a production build:** React StrictMode double-invokes
  effects, which briefly opens more than one `EventSource` to the same route and can log a
  "two children with the same key" console warning during the overlap. Verified this does NOT
  reproduce in `next build && next start` (checked via network request counts — one connection
  per feed, not several — and the rendered output, which was correct in dev too; only the dev
  console warning differed). Not a production bug; not chased further than confirming that.
