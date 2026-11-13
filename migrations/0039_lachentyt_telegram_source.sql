\set ON_ERROR_STOP on
BEGIN;

WITH platform AS (
  SELECT id FROM platforms WHERE key = 'telegram' AND enabled
)
INSERT INTO sources(
  legacy_id, origin, platform_id, name, url, enabled, original_id, is_official_dsns
)
SELECT
  'tg_lachentyt',
  'general',
  platform.id,
  'Лачен пише',
  'https://t.me/lachentyt',
  true,
  'tg_lachentyt',
  false
FROM platform
ON CONFLICT(origin, platform_id, legacy_id) DO UPDATE
SET name = EXCLUDED.name,
    url = EXCLUDED.url,
    enabled = true,
    is_official_dsns = false,
    updated_at = now();

INSERT INTO collector_health(source_id, next_check_at, priority, poll_interval_seconds)
SELECT s.id, now(), 200, 60
FROM sources s
JOIN platforms p ON p.id = s.platform_id
WHERE p.key = 'telegram'
  AND s.origin = 'general'
  AND s.legacy_id = 'tg_lachentyt'
ON CONFLICT(source_id) DO UPDATE
SET next_check_at = LEAST(collector_health.next_check_at, now()),
    priority = 200,
    poll_interval_seconds = 60;

INSERT INTO argus_shadow_source_state(source_id, priority, poll_interval_seconds)
SELECT ch.source_id, ch.priority, ch.poll_interval_seconds
FROM collector_health ch
JOIN sources s ON s.id = ch.source_id
WHERE s.origin = 'general' AND s.legacy_id = 'tg_lachentyt'
ON CONFLICT(source_id) DO UPDATE
SET priority = EXCLUDED.priority,
    poll_interval_seconds = EXCLUDED.poll_interval_seconds;

COMMIT;
