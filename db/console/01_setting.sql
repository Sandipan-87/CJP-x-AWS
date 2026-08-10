-- Engram P0-P1 · chunk 1/9 (REVISED) · feature gate, read-only
-- The Console SQL Shell DISALLOWS `SET CLUSTER SETTING` (SQLSTATE XXUUU,
-- "disallowed statement type"). That is a console restriction, not a cluster
-- one, and it does not matter: chunk 4's CREATE VECTOR INDEX already succeeded,
-- which proves the feature is enabled. Just read the value.

SHOW CLUSTER SETTING feature.vector_index.enabled;
