-- Engram · TARGET cluster migration 001 — engram_probe / engram_operator roles.  [PLUMBER]
-- design/01-high-level-design.md D6/ADR-006, design/02-low-level-design.md §2
-- (ENGRAM_TARGET_PROBE_DSN / ENGRAM_TARGET_OPERATOR_DSN).
--
-- SEPARATE migration lineage from db/migrations/ — those apply to the MEMORY cluster (the
-- product); this applies to the TARGET cluster (the subject being operated on). Never conflate
-- the two (CLAUDE.md §2: "two clusters, two roles, never conflate them").
--
-- No passwords here on purpose — CREATE ROLE below has no LOGIN/PASSWORD clause, so this file
-- has nothing secret in it and is safe to commit. `scripts/bootstrap_target_roles.py` runs
-- `ALTER ROLE ... WITH LOGIN PASSWORD` as a separate statement (via psycopg.sql.Literal, never
-- string-interpolated) immediately after this file's statements, then writes the resulting DSNs
-- straight into `.env` — the password itself is never written to a file this repo tracks.
--
-- Run as the target cluster's admin role (`engram_admin` — the same one `ENGRAM_TARGET_DSN`
-- already authenticates as).

-- engram_probe: read/introspect only (HLD D6/§4: "SELECT, EXPLAIN ANALYZE"). EXPLAIN ANALYZE
-- needs no privilege beyond SELECT on the tables it touches — it runs the real query and reports
-- execution stats, it is not a separately privileged operation in CockroachDB.
CREATE ROLE IF NOT EXISTS engram_probe;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO engram_probe;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA public GRANT SELECT ON TABLES TO engram_probe;

-- engram_operator: allowlisted DDL only (HLD §4, verbatim: "CREATE INDEX, ANALYZE... no
-- DROP/TRUNCATE/GRANT"). CREATE INDEX needs the CREATE privilege on the table; ANALYZE needs
-- SELECT. Deliberately NOT granted: INSERT/UPDATE/DELETE, DROP, GRANT OPTION, or CREATE at the
-- schema/database level (which would additionally allow CREATE TABLE — out of scope for this
-- role; `recipe_renderer.py`'s own allowlist is `create_index`/`analyze_table` only).
CREATE ROLE IF NOT EXISTS engram_operator;
GRANT SELECT, CREATE ON ALL TABLES IN SCHEMA public TO engram_operator;
ALTER DEFAULT PRIVILEGES FOR ROLE engram_admin IN SCHEMA public
  GRANT SELECT, CREATE ON TABLES TO engram_operator;
