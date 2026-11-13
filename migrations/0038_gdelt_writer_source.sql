\set ON_ERROR_STOP on
BEGIN;

-- Official GDELT DOC 2.0 endpoint. The collector adds bounded time-window,
-- result-mode and pagination parameters at request time.
WITH platform AS (
  SELECT id FROM platforms WHERE key='gdelt' AND enabled
)
INSERT INTO sources(
  legacy_id,origin,platform_id,name,url,enabled,original_id,is_official_dsns
)
SELECT
  'gdelt_ukraine_doc_v1',
  'general',
  platform.id,
  'GDELT Ukraine DOC API',
  'https://api.gdeltproject.org/api/v2/doc/doc?query=ukraine',
  true,
  'gdelt_ukraine_doc_v1',
  false
FROM platform
ON CONFLICT(origin,platform_id,legacy_id) DO UPDATE
SET enabled=true, updated_at=now();

INSERT INTO collector_health(source_id,next_check_at,priority,poll_interval_seconds)
SELECT s.id,now(),100,300
FROM sources s
JOIN platforms p ON p.id=s.platform_id
WHERE p.key='gdelt'
  AND s.origin='general'
  AND s.legacy_id='gdelt_ukraine_doc_v1'
ON CONFLICT(source_id) DO UPDATE
SET next_check_at=LEAST(collector_health.next_check_at,now());

INSERT INTO argus_shadow_source_state(source_id,priority,poll_interval_seconds)
SELECT ch.source_id,ch.priority,ch.poll_interval_seconds
FROM collector_health ch
JOIN sources s ON s.id=ch.source_id
JOIN platforms p ON p.id=s.platform_id
WHERE p.key='gdelt' AND s.legacy_id='gdelt_ukraine_doc_v1'
ON CONFLICT(source_id) DO NOTHING;

COMMIT;
