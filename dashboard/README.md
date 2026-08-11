# Engram Dashboard — read-only SSE surface

[ILLUSIONIST]. `design/02-low-level-design.md` §11 (frozen contract) / HLD §5.6.

**Scope, updated:** the four read-only SSE feeds (§11.1) plus the mutation path (§11.2) are both
now wired. `src/app/api/approvals/[approvalId]/route.ts` is the backend-for-frontend hop: it
holds the real API Gateway key server-side only (`ENGRAM_APPROVALS_API_KEY`) and proxies the
browser's request to `infra/`'s deployed endpoint. The browser itself never talks to API Gateway
directly and never sees the key — same reasoning as `engram_reader` being the only DB credential
this app ever holds. Approve/Reject in `ApprovalQueuePanel.tsx` call this route for real; if
`ENGRAM_APPROVALS_API_URL`/`ENGRAM_APPROVALS_API_KEY` aren't set (nothing deployed yet), the
route returns 503 and the button shows that as an inline error — not a special case, just what
"the request failed" looks like.

**Live-verified twice: once against a local shim, once against the real deployed AWS stack.**
First pass used `scripts/local_approvals_api_shim.py` (a local dev-only stand-in for API Gateway
that runs the real `workers/approvals/handler.py` code) — seeded a real pending approval, clicked
the real Approve button, watched a real `200` come back, and watched the Approval Queue AND
Action Feed panels update to "approved" on their own via the SSE feed, no page reload. That run
caught a real bug (see `workers/approvals/handler.py`'s own comment): a non-UUID `approval_id`
crashed the handler instead of returning 400; fixed and covered by a new test. **Then, once
`infra/` was actually deployed (2026-08-12, `infra/README.md`), the same click was repeated
against the real API Gateway + Lambda in AWS** — real `200`, and a direct DB query afterward
confirmed `status='approved'`, `channel='dashboard'` exactly per LLD §11.2/§11.3.
`dashboard/.env.local` now points at the real endpoint by default; `ENGRAM_APPROVALS_API_URL=
http://localhost:8787` + `ENGRAM_APPROVALS_API_KEY=local-dev-key` (the shim) remains available
for offline testing.

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
5. For the mutation path, set in `.env.local` (gitignored):
   ```
   ENGRAM_APPROVALS_API_URL=<infra/'s deployed API Gateway URL, once deployed>
   ENGRAM_APPROVALS_API_KEY=<the real API Gateway key -- see infra/README.md>
   ```
   Or, for local testing without deploying anything to AWS,
   `python scripts/local_approvals_api_shim.py` (repo root) runs the real Lambda handler code as
   a local HTTP server and set:
   ```
   ENGRAM_APPROVALS_API_URL=http://localhost:8787
   ENGRAM_APPROVALS_API_KEY=local-dev-key
   ```

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
