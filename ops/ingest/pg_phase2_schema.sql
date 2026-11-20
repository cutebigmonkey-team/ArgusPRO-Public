-- REVIEW ONLY. Apply exclusively to an explicitly authorized private database.
-- No runtime migration, role creation, credentials or production bindings.
BEGIN;
CREATE TABLE argus_ingest.checkpoint_groups (
 sequence bigserial UNIQUE NOT NULL, group_id text PRIMARY KEY, slot text UNIQUE NOT NULL, manifest bytea NOT NULL,
 source_id text NOT NULL, base_revision bigint NOT NULL CHECK(base_revision>=0),
 proposal jsonb, part_count integer NOT NULL CHECK(part_count BETWEEN 1 AND 256),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','delivered','cursor_conflict'))
);
CREATE TABLE argus_ingest.source_cursors (
 source_id text PRIMARY KEY, revision bigint NOT NULL DEFAULT 0 CHECK(revision>=0),
 checkpoint jsonb, verified_group text REFERENCES argus_ingest.checkpoint_groups(group_id)
);
CREATE TABLE argus_ingest.oidc_replays (
 jti_hash char(64) PRIMARY KEY, batch_id text NOT NULL, body_sha char(64) NOT NULL,
 expires_at timestamptz NOT NULL
);
ALTER TABLE argus_ingest.batches ADD COLUMN group_id text REFERENCES argus_ingest.checkpoint_groups(group_id);
ALTER TABLE argus_ingest.batches ADD COLUMN part_index integer;
ALTER TABLE argus_ingest.batches ADD COLUMN available_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE argus_ingest.batches ADD COLUMN last_error text;
ALTER TABLE argus_ingest.batches DROP CONSTRAINT batches_state_check;
ALTER TABLE argus_ingest.batches ADD CONSTRAINT batches_state_check CHECK(state IN ('pending','leased','acknowledged','dead'));
CREATE UNIQUE INDEX batches_group_part ON argus_ingest.batches(group_id,part_index) WHERE group_id IS NOT NULL;
REVOKE ALL ON argus_ingest.checkpoint_groups,argus_ingest.source_cursors,argus_ingest.oidc_replays FROM PUBLIC;
-- No GRANTs: ingress-only security-definer procedures/roles require private review.

ALTER TABLE argus_ingest.batches ADD CONSTRAINT phase2_part_identity CHECK
 ((group_id IS NULL AND part_index IS NULL) OR (group_id IS NOT NULL AND part_index BETWEEN 0 AND 255));

COMMIT;
