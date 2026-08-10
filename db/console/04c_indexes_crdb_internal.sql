-- Engram P0-P1 · chunk 4c · index artifact, FALLBACK 1
-- `SHOW INDEXES FROM vec_probe` returns "Internal error" on a table carrying a
-- vector index (trace id 11785372470624755431, CockroachDB CCL v26.2.1).
-- Read the catalog directly instead. SELECT * on purpose — do not guess column
-- names and risk a second error.

SELECT * FROM crdb_internal.table_indexes WHERE descriptor_name = 'vec_probe';
