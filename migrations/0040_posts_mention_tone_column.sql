\set ON_ERROR_STOP on
BEGIN;

-- Persists the DSNS-mention tone classification that /api/posts and
-- /api/analytics/summary previously recomputed per request via a regex CASE
-- expression over lower(text||' '||content). Measured against production:
-- that per-request regex scan takes 22-56s over 170k-520k rows (Postgres
-- must decompress TOASTed text/content for every row; the existing trigram
-- index on the bare `text` column doesn't cover the concatenated
-- expression), which starves the whole site given DB_POOL_SIZE=1 on the
-- web container. A plain nullable column add is metadata-only in Postgres
-- (no table rewrite) — existing rows start NULL and are backfilled
-- separately in batches, see ops/backfill-mention-tone.sh. New/re-collected
-- rows get it computed by Normalizer.cpp's INSERT going forward.
ALTER TABLE posts ADD COLUMN IF NOT EXISTS mention_tone TEXT;

COMMIT;