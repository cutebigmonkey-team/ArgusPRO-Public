-- REVIEW ONLY. Do not apply automatically.
-- Requires the old Argus PostgreSQL schema through 0033 and pgcrypto.

CREATE TABLE IF NOT EXISTS argus_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_type        TEXT NOT NULL,
  source_id       UUID REFERENCES sources(id) ON DELETE CASCADE,
  payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','completed','retry','dead_letter')),
  priority        INTEGER NOT NULL DEFAULT 100,
  attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts    INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
  available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  locked_at       TIMESTAMPTZ,
  locked_by       TEXT,
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at    TIMESTAMPTZ,
  last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_argus_jobs_claim
  ON argus_jobs (status, available_at, priority DESC, created_at)
  WHERE status IN ('pending','retry');

CREATE INDEX IF NOT EXISTS idx_argus_jobs_source
  ON argus_jobs (source_id, created_at DESC);

-- Shadow scheduling is intentionally isolated from production collector_health.
CREATE TABLE IF NOT EXISTS argus_shadow_source_state (
  source_id             UUID PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  next_check_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  priority              INTEGER NOT NULL DEFAULT 100,
  poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
  claimed_at            TIMESTAMPTZ,
  claimed_by            TEXT,
  last_checked_at       TIMESTAMPTZ,
  last_status           TEXT,
  last_error            TEXT
);
INSERT INTO argus_shadow_source_state(source_id,priority,poll_interval_seconds)
SELECT s.id,CASE WHEN s.origin='dsns' THEN 150 ELSE 100 END,
       CASE WHEN s.origin='dsns' THEN 120 ELSE 300 END
FROM sources s WHERE s.enabled ON CONFLICT(source_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS argus_shadow_reports (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id          UUID REFERENCES sources(id) ON DELETE CASCADE,
  job_id             UUID REFERENCES argus_jobs(id) ON DELETE SET NULL,
  stage              TEXT NOT NULL CHECK (stage IN ('collect','normalize')),
  status             TEXT NOT NULL CHECK (status IN ('ok','failed')),
  observed_count     INTEGER NOT NULL DEFAULT 0,
  canonical_key      TEXT,
  normalized_payload JSONB,
  error_code         TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_argus_shadow_reports_source
  ON argus_shadow_reports(source_id,created_at DESC);

-- These operational tables live in Supabase's API-exposed public schema.
-- Keep queue payloads and worker state unavailable to browser/API roles;
-- the VM workers connect with the protected database credential.
ALTER TABLE argus_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE argus_shadow_source_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE argus_shadow_reports ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE argus_jobs FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE argus_shadow_source_state FROM PUBLIC, anon, authenticated;
REVOKE ALL ON TABLE argus_shadow_reports FROM PUBLIC, anon, authenticated;

-- Canonical identity protects the home-PC → VM transition. Existing
-- posts.dedup_key remains the authoritative legacy constraint.
ALTER TABLE posts
  ADD COLUMN IF NOT EXISTS canonical_key TEXT,
  ADD COLUMN IF NOT EXISTS idempotency_key TEXT;

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_posts_canonical_key
  ON posts (canonical_key) WHERE canonical_key IS NOT NULL;
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_posts_idempotency_key
  ON posts (idempotency_key) WHERE idempotency_key IS NOT NULL;
