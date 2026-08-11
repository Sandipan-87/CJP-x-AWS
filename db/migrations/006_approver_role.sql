-- Engram · Migration 006 — engram_approver role for the dashboard's approvals Lambda.  [PLUMBER]
-- design/02-low-level-design.md §11.2 (POST /approvals/{approval_id}).
--
-- Neither existing dashboard-adjacent role fits: `engram_agent` (rw) is far broader than this
-- Lambda needs, and `engram_reader` (SELECT-only, migrations 002/005) cannot perform the CAS
-- UPDATE §11.2 requires. HLD's own secrets table loosely groups "memory-reader-dsn" under
-- "(dashboard Lambda)" but that's imprecise for the approvals Lambda specifically -- SELECT-only
-- cannot decide an approval. A dedicated role matching exactly what this one Lambda does (and
-- nothing else) keeps the same least-privilege discipline already applied to
-- engram_probe/engram_operator/engram_reader.
--
-- SELECT is needed alongside UPDATE so the Lambda can distinguish 404 (approval_id doesn't
-- exist) from 409 (exists, already decided) after a CAS UPDATE affects 0 rows -- LLD §11.2's own
-- distinction. No passwords here -- see scripts/bootstrap_approver_role.py.

CREATE ROLE IF NOT EXISTS engram_approver;
GRANT SELECT, UPDATE ON approvals TO engram_approver;
