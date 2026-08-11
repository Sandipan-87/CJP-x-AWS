import { Pool } from "pg";
import fs from "node:fs";
import path from "node:path";

// Server-side ONLY -- this module must never be imported from a "use client" file.
// HLD §5.6: "No DB credentials in the frontend; only the engram_reader DSN is present
// in the serverless function (read-only)." engram_reader is SELECT-only on
// v_recent_tasks/v_action_feed/v_memory_inspector/observations/approvals
// (db/migrations/002_grants.sql + 005_reader_approvals_grant.sql) -- there is no write
// path through this pool, by construction, not by convention.

let pool: Pool | null = null;

export function getReaderPool(): Pool {
  if (pool) return pool;

  const dsn = process.env.ENGRAM_READER_DSN;
  if (!dsn) {
    throw new Error(
      "ENGRAM_READER_DSN not set -- see dashboard/README.md (scripts/bootstrap_reader_role.py " +
        "provisions it in the repo-root .env; copy the value into dashboard/.env.local)."
    );
  }

  // CockroachDB Cloud's cert chains to a public root (ISRG Root X1); verify-full needs it
  // explicit because pg's own bundled trust store doesn't include it by default on every
  // platform -- same class of issue scripts/run_sql.py's own --sslrootcert flag exists for.
  const caPath = path.join(process.cwd(), "certs", "memory-ca.crt");
  const ca = fs.existsSync(caPath) ? fs.readFileSync(caPath, "utf-8") : undefined;
  if (!ca) {
    console.warn(
      "WARN: dashboard/certs/memory-ca.crt not found -- falling back to rejectUnauthorized:false. " +
        "See dashboard/README.md to fetch the real cert; never rely on this fallback past local dev."
    );
  }

  pool = new Pool({
    connectionString: dsn,
    ssl: ca ? { ca, rejectUnauthorized: true } : { rejectUnauthorized: false },
    max: 5,
  });
  return pool;
}
