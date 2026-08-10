-- Engram P0-P1 · chunk 4b · KNOWN TO FAIL — kept as the reproduction case.
--
-- On CockroachDB CCL v26.2.1 (Cloud Basic), this statement returns:
--     Internal error (trace id: 11785372470624755431)
-- when the table carries a VECTOR INDEX.
--
-- This is a reproducible defect in CockroachDB's index introspection on
-- vector-indexed tables, and it is almost certainly the same root cause as
-- SHOW CREATE TABLE omitting the VECTOR INDEX line. It is a DISPLAY-layer
-- problem only: the index itself is fully functional (see §1.3 — the planner
-- resolves vec_probe@vec_probe_scope_cos and reads 11 of 400 rows).
--
-- Keep this file. It is submission material: the hackathon rubric invites tool
-- feedback (strategy §21), and a minimal reproduction beats a bug report.
--
-- For the actual artifact use 04c (crdb_internal) or 04d (pg_catalog).

SHOW INDEXES FROM vec_probe;
