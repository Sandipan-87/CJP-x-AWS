-- Engram P0-P1 · chunk 9/9 · CLEANUP
-- !! DO NOT RUN THIS UNTIL verify_mcp.py HAS FINISHED !!
-- Its LIMIT-25 and explain_query probes need vec_probe to still exist.
-- vec_probe must not survive into Phase 1.

DROP TABLE vec_probe;
