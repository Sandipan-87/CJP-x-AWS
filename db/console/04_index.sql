-- Engram P0-P1 · chunk 4/9 · create the C-SPANN index AFTER seeding
-- CLAUDE.md invariant #2 — exact shape, do not paraphrase.
-- SHOW CREATE TABLE output here is the proof artifact. Paste all of it.

CREATE VECTOR INDEX vec_probe_scope_cos
    ON vec_probe (scope_id, embedding vector_cosine_ops)
    WITH (min_partition_size = 16, max_partition_size = 128);

SHOW INDEXES FROM vec_probe;

SHOW CREATE TABLE vec_probe;
