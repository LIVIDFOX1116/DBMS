-- FrameCheck migration (2026-10): engaged_proximity detector's algorithm
-- catalog entry. Same pattern as 003_framecheck_detectors.sql -- each
-- detector run creates its own analysis_run row via start_run/finish_run,
-- which requires an algorithm_id to exist first.

BEGIN;

INSERT INTO algorithm (algorithm_name, category, cugraph_function, library_version)
VALUES
    ('engaged_proximity', 'detector', NULL, 'TBD')
ON CONFLICT (algorithm_name) DO NOTHING;

COMMIT;
