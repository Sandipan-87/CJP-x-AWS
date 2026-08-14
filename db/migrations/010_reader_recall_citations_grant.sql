-- Engram · Migration 010 — engram_reader gains read access to recall citations/similarity.  [PLUMBER]
--
-- Closes the gap CLAUDE.md's submission checklist has carried since the dashboard chunk (Session
-- 30/§11.1): the frozen `v_memory_inspector` view (migration 001) only ever carried
-- `confidence`/`provenance` — the "it remembers" demo beat's own similarity score lives in
-- `decisions.citations` (JSONB, `[{memory_item_id, score, source}]`, written by
-- agent/nodes/recall.py), which engram_reader had no grant on at all.
--
-- Deliberately NOT granting SELECT on the raw `decisions` table (unlike `observations`/
-- `approvals`, which LLD §11.1 names directly): `decisions` also carries `reasoning` JSONB for
-- every node (reason/gate/act too), which is more than this one demo beat needs exposed to the
-- read-only dashboard role. Instead, a narrow view unnests just the recall citations into
-- (item_id, similarity, scope_id, cited_at) rows — one row per cited memory item per recall call
-- — which the dashboard can join against `v_memory_inspector`'s own `item_id` client-side.
--
-- The window-function ROW_NUMBER()/rn=1 filter keeps only the MOST RECENT citation per
-- memory_item_id (a procedure/episode can be cited by many recall calls over time; the demo only
-- wants the latest similarity score on screen, not a growing history per item). A plain
-- `DISTINCT ON` was tried first and rejected live by CockroachDB (`InvalidColumnReference: SELECT
-- DISTINCT ON expressions must match initial ORDER BY expressions`, even with textually-identical
-- expressions) -- ROW_NUMBER over a subquery is the portable equivalent. Column aliased
-- `created_at`, not `cited_at` — so the dashboard's existing `createdAtFeed()` helper
-- (src/lib/feeds.ts, hardcodes a `created_at` cursor column) works unmodified, matching every
-- other frozen-feed view's own convention.
CREATE VIEW v_recall_citations AS
  SELECT item_id, similarity, source, scope_id, created_at
  FROM (
    SELECT
      (elem->>'memory_item_id')::UUID AS item_id,
      (elem->>'score')::FLOAT8 AS similarity,
      elem->>'source' AS source,
      d.scope_id,
      d.created_at AS created_at,
      ROW_NUMBER() OVER (PARTITION BY (elem->>'memory_item_id') ORDER BY d.created_at DESC) AS rn
    FROM decisions d, jsonb_array_elements(d.citations) AS elem
    WHERE d.node = 'recall'
  ) ranked
  WHERE rn = 1
  ORDER BY created_at DESC
  LIMIT 500;

GRANT SELECT ON v_recall_citations TO engram_reader;
