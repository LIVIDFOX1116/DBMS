-- FrameCheck migration (2026-10): detector algorithm catalog entries
-- (Phase 3). Each detector run creates its own analysis_run row via the
-- SAME start_run/finish_run graph/run_analysis.py already uses for
-- pagerank/louvain/bfs (see graph/detectors.py) -- that FK requires an
-- algorithm_id to exist first. Idempotent: ON CONFLICT DO NOTHING, same
-- pattern schema.sql's own algorithm seed uses.

BEGIN;

INSERT INTO algorithm (algorithm_name, category, cugraph_function, library_version)
VALUES
    ('bfs_proximity',      'detector', NULL, 'TBD'),
    ('sybil_cluster',      'detector', NULL, 'TBD'),
    ('pagerank_threshold', 'detector', NULL, 'TBD'),
    ('impurity',           'detector', NULL, 'TBD')
ON CONFLICT (algorithm_name) DO NOTHING;

COMMIT;
