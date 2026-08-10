-- Engram P0-P1 · chunk 2/9 · probe table, NO INDEX YET
-- CLAUDE.md invariant #1: seed rows BEFORE creating the vector index.

DROP TABLE IF EXISTS vec_probe;

CREATE TABLE vec_probe (
    id        INT8 PRIMARY KEY,
    scope_id  STRING    NOT NULL,
    label     STRING,
    embedding VECTOR(1024)
);

SHOW CREATE TABLE vec_probe;
