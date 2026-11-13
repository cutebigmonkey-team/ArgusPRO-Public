\set ON_ERROR_STOP on
BEGIN;

-- Additive reference rows for collector runtimes that do not yet own live
-- sources. Applying this migration does not move, enable, or enqueue a source.
INSERT INTO platforms(key,name,requires_auth,enabled) VALUES
  ('gdelt','GDELT',false,true),
  ('dsns-http','DSNS HTTP',false,true),
  ('dsns-browser','DSNS Browser',false,true)
ON CONFLICT(key) DO NOTHING;

-- Opaque per-source cursors are shadow-only state. GDELT stores the end of
-- the last successfully collected time window here; failed runs do not move
-- it forward and the next successful run overlaps by 15 minutes.
ALTER TABLE argus_shadow_source_state
  ADD COLUMN IF NOT EXISTS cursor JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Existing enabled sources added since 0034 also need isolated shadow state.
INSERT INTO argus_shadow_source_state(source_id,priority,poll_interval_seconds)
SELECT s.id,
       CASE WHEN s.origin='dsns' THEN 150 ELSE 100 END,
       CASE WHEN s.origin='dsns' THEN 120 ELSE 300 END
FROM sources s
WHERE s.enabled
ON CONFLICT(source_id) DO NOTHING;

COMMIT;
