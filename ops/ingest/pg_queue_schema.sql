-- MANUAL REVIEW ONLY: do not apply in production or GitHub Actions.
-- Create in an isolated staging PostgreSQL database, with a dedicated DB role.
CREATE SCHEMA IF NOT EXISTS argus_ingest;
CREATE TABLE IF NOT EXISTS argus_ingest.batches (
    run_id text PRIMARY KEY,
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    body bytea NOT NULL CHECK (octet_length(body) <= 2000000),
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','leased','acknowledged')),
    received_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token text,
    lease_until timestamptz,
    acknowledged_at timestamptz,
    CHECK ((state='leased' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
       OR (state<>'leased' AND lease_token IS NULL AND lease_until IS NULL))
);
CREATE INDEX IF NOT EXISTS batches_lease_idx
    ON argus_ingest.batches (received_at,run_id) WHERE state='pending';
CREATE INDEX IF NOT EXISTS batches_expiry_idx
    ON argus_ingest.batches (lease_until) WHERE state='leased';
-- SECURITY: Do not give the public ingress role SELECT/UPDATE of lease_token,
-- acknowledge or broad SELECT privileges. This first prototype deliberately
-- has no GRANT statements; finalize separate ingress and worker roles before deploy.
