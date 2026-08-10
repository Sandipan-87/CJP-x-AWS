-- Engram P0-P1 · chunk 3/9 · seed 400 rows across 2 scopes
-- Correlated on g.i so every row gets a distinct vector.
-- Expect: org-alpha 200, org-beta 200.

INSERT INTO vec_probe (id, scope_id, label, embedding)
SELECT
    g.i,
    CASE WHEN g.i % 2 = 0 THEN 'org-alpha' ELSE 'org-beta' END,
    'seed-' || g.i::STRING,
    (
      '[' ||
      (SELECT string_agg(
                (sin((g.i::FLOAT8 * 0.013) + (d.k::FLOAT8 * 0.7)) * 0.5 + cos(d.k::FLOAT8 * 0.11) * 0.5)::STRING,
                ',' ORDER BY d.k)
         FROM generate_series(1, 1024) AS d(k))
      || ']'
    )::VECTOR(1024)
FROM generate_series(1, 400) AS g(i);

SELECT scope_id, count(*) AS rows FROM vec_probe GROUP BY scope_id ORDER BY scope_id;
