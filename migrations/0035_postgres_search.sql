-- REVIEW ONLY. Do not apply automatically.
-- Run with psql autocommit: CREATE INDEX CONCURRENTLY cannot run in a
-- transaction block. This avoids a generated STORED column and its full
-- 2+ GiB table rewrite; the FTS document is indexed as an expression.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posts_search_document_expr
  ON posts USING GIN ((
    setweight(to_tsvector('simple', coalesce(source_name, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(text, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(content, '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(category, '')), 'C')
  ));

-- The old query searches these short identity fields with substring ILIKE.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sources_name_trgm
  ON sources USING GIN (name gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_sources_legacy_id_trgm
  ON sources USING GIN (legacy_id gin_trgm_ops);

-- Keyset-pagination/filter support. The trailing id gives a deterministic
-- cursor when multiple posts share the same timestamp.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posts_published_id
  ON posts (published_at DESC, id DESC)
  WHERE published_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posts_platform_published_id
  ON posts (platform_id, published_at DESC, id DESC)
  WHERE published_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posts_region_published_id
  ON posts (region_id, published_at DESC, id DESC)
  WHERE published_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_posts_source_published_id
  ON posts (source_id, published_at DESC, id DESC)
  WHERE source_id IS NOT NULL AND published_at IS NOT NULL;
