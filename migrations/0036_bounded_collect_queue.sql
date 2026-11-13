-- Additive queue invariant. Run with psql autocommit because CONCURRENTLY
-- cannot execute inside a transaction block.
--
-- Precondition: ops/rss-shadow-deduplicate-active-jobs.sql has reduced any
-- pre-existing shadow duplicates to one active collect job per source.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_argus_jobs_active_collect_source_mode
  ON argus_jobs (source_id, (coalesce(payload->>'mode', 'writer')))
  WHERE job_type = 'collect_source'
    AND status IN ('pending', 'retry', 'processing');
