-- Engram P0-P1 · chunk 4d · index artifact, FALLBACK 2
-- Postgres-compatibility view. Try this if 4c also errors.
-- Note: pg_indexes may omit vector indexes entirely rather than erroring —
-- an empty result here is NOT evidence the index is missing.

SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'vec_probe';
