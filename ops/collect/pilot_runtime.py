"""Read-only lane adapter over run.py; never a delivery or checkpoint owner.

Cursor proposals in report.json are observations, not committed checkpoints.
An authenticated ingestion service and verified archive read-back are deliberately
required before a future implementation may enable durable cursor advancement.
"""
import argparse
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile

LANES = {
    'fast': frozenset(('telegram', 'rss', 'youtube', 'threads', 'tiktok',
                       'rutube', 'mailru', 'reddit', 'gdelt')),
    'extended': frozenset(('facebook', 'instagram', 'vk', 'ok', 'x')),
}


def assignment(sources, lane, shard, shards, turn=0):
    if lane not in LANES or not 0 <= shard < shards <= 32:
        raise ValueError('invalid_lane_assignment')
    seen = set()
    selected = []
    for source in sources:
        sid = source.get('id')
        if not isinstance(sid, str) or not sid or sid in seen:
            raise ValueError('invalid_source_identity')
        seen.add(sid)
        if source.get('platform') not in set().union(*LANES.values()):
            raise ValueError('unsupported_platform')
        if source.get('enabled', True) is not True:
            continue
        if source['platform'] in LANES[lane] and int(hashlib.sha256(sid.encode()).hexdigest(), 16) % shards == shard:
            selected.append(source)
    # Rotate each platform independently: a repeatedly exhausted budget must
    # not always leave the same tail unattempted. Membership/shards stay stable.
    rotated = []
    for platform in sorted(LANES[lane]):
        group = sorted((s for s in selected if s['platform'] == platform), key=lambda s: s['id'])
        if group:
            offset = turn % len(group)
            rotated.extend(group[offset:] + group[:offset])
    return rotated


def advance_checkpoint(*args, **kwargs):
    # No boolean or caller-provided JSON receipt is an authenticated read-back.
    # There is intentionally no filesystem/database write path in Phase 1.
    raise RuntimeError('authenticated_archive_readback_not_implemented')


def coverage(selected, report, posts, failed=False, now=None):
    now = now or datetime.now(timezone.utc)
    rows = report.get('sources', [])
    expected = {s['id'] for s in selected}
    observed = {}
    for row in rows:
        sid = row.get('id', row.get('source_id'))
        if sid not in expected or sid in observed:
            raise ValueError('invalid_collector_source_accounting')
        observed[sid] = row
    platforms = {}
    for source in selected:
        row = observed.get(source['id'], {})
        status = row.get('status', 'collector_failed' if failed else 'unreported')
        allowed_statuses = {'ok', 'no_new_posts', 'no_public_posts', 'partial_results',
                            'publisher_only', 'deferred_budget', 'deferred_rate_limit',
                            'disabled_by_registry', 'not_due', 'http_429', 'rate_limited',
                            'gateway_rate_limited', 'login_required', 'http_401',
                            'http_403', 'challenge_required', 'robots_denied',
                            'timeout', 'parser_error', 'collector_failed', 'unreported'}
        if status not in allowed_statuses:
            status = 'other_collector_error'
        stats = platforms.setdefault(source['platform'], {
            'assigned': 0, 'started': 0, 'reported': 0, 'statuses': Counter(),
            'extracted_records': 0, 'dated_records': 0,
            'publication_age_le_5m': 0, 'publication_age_le_15m': 0, 'publication_age_le_30m': 0,
            'publication_age_le_60m': 0,
            'new_archived_posts': 'NOT_MEASURABLE',
            'archive_writes': 'NOT_MEASURABLE',
            'frontend_visible_posts': 'NOT_MEASURABLE',
            'sources_with_posts': 0, 'dated_sources': 0,
            'sources_fresh_5m': 0, 'sources_fresh_15m': 0, 'sources_fresh_60m': 0,
            'latest_source_age_s_median': None, 'reader_attempts': {},
            '_source_ages': [],
        })
        stats['assigned'] += 1
        stats['reported'] += bool(row)
        stats['started'] += bool(row.get('started_at'))
        stats['statuses'][status] += 1
        stats['sources_with_posts'] += row.get('posts', 0) > 0
        stamp = row.get('latest_published_at')
        if isinstance(stamp, str):
            try:
                published = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                age = (now - published).total_seconds() if published.tzinfo else -1
                if age >= 0:
                    stats['_source_ages'].append(int(age))
                    stats['dated_sources'] += 1
                    for seconds, key in ((300, 'sources_fresh_5m'), (900, 'sources_fresh_15m'), (3600, 'sources_fresh_60m')):
                        stats[key] += age <= seconds
            except (ValueError, TypeError, OverflowError):
                pass
        allowed_tools = {'anonymous_policy', 'detail_enrichment', 'enhanced_web',
                         'gdelt_public_json', 'instaloader', 'playwright_article',
                         'public_embed', 'publisher_tiktok_embed', 'rss_bridge', 'rsshub',
                         'source_deadline', 'source_validation', 'telegram_public_head',
                         'telegram_public_history', 'threads_public_json', 'tinyfish_fetch',
                         'yt-dlp', 'sipher', 'tiktok-cli', 'feeds', 'playwright',
                         'gallery-dl', 'granary', 'fbgql', 'skraper', 'vkrss', 'socialfetch'}
        for attempt in row.get('attempts', []):
            tool = attempt.get('tool')
            tool = tool if tool in allowed_tools else 'other_reader'
            attempt_status = attempt.get('status')
            attempt_status = attempt_status if attempt_status in allowed_statuses else 'other_collector_error'
            counts = stats['reader_attempts'].setdefault(tool, {})
            counts[attempt_status] = counts.get(attempt_status, 0) + 1
    for post in posts:
        stats = platforms.get(post.get('platform'))
        if stats is None:
            raise ValueError('unexpected_post_platform')
        stats['extracted_records'] += 1
        stamp = post.get('published_at')
        if not isinstance(stamp, str) or post.get('published_at_estimated') or post.get('published_at_is_estimated') or post.get('date_estimated'):
            continue
        try:
            published = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if published.tzinfo is None:
                continue
            age = (now - published).total_seconds()
        except (ValueError, TypeError):
            continue
        if age < 0:
            continue
        stats['dated_records'] += 1
        for seconds, key in ((300, 'publication_age_le_5m'), (900, 'publication_age_le_15m'), (1800, 'publication_age_le_30m'), (3600, 'publication_age_le_60m')):
            stats[key] += age <= seconds
    for stats in platforms.values():
        ages = stats.pop('_source_ages')
        stats['latest_source_age_s_median'] = median(ages) if ages else None
    return {'mode': 'pilot_read_only', 'checkpoint_advanced': False,
            'delivery': 'BLOCKED_PRIVATE_INGESTION_SERVICE', 'platforms': platforms,
            'unreported': len(expected - observed.keys())}


def execute(args, runner=subprocess.run):
    catalog = json.loads(Path(args.sources).read_text())
    selected = assignment(catalog['sources'], args.lane, args.shard_index, args.shard_count,
                          int(datetime.now(timezone.utc).timestamp()) //
                          {'fast': 300, 'extended': 900}[args.lane])
    output = Path(args.output)
    # Never mix receipts from an earlier invocation with this run's observations.
    if output.exists() and any(output.iterdir()):
        raise ValueError('output_must_be_empty')
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    code = 0
    with tempfile.TemporaryDirectory(prefix='argus-pilot-') as scratch:
        source_path = Path(scratch) / 'sources.json'
        source_path.write_text(json.dumps({'sources': selected}))
        source_path.chmod(0o600)
        # The native hash guard is idempotent for this already assigned subset.
        # Preserve the original shard coordinates: run.py also uses them to
        # divide optional upstream quotas across shards. Keep native pacing,
        # per-source deadlines, incremental paths and platform worker isolation.
        if selected:
            command = [sys.executable, str(Path(__file__).with_name('run.py')),
                       '--sources', str(source_path), '--output', str(output),
                       '--sources-per-platform', '0', '--posts', '5',
                       '--incremental', '--adaptive', '--telegram-history',
                       '--batch-seconds', '180', '--source-timeout', '30',
                       '--concurrency', '2', '--shard-index', str(args.shard_index),
                       '--shard-count', str(args.shard_count)]
            # Collector stdout is not a trusted public log interface.
            with (Path(scratch) / 'collector.log').open('w') as log:
                try:
                    code = runner(command, stdout=log, stderr=log, timeout=240, check=False).returncode
                except subprocess.TimeoutExpired:
                    code = 124
                except OSError:
                    code = 125
        report_path = output / 'report.json'
        posts_path = output / 'posts.jsonl'
        if selected and not (report_path.exists() and posts_path.exists()):
            code = code or 1
        try:
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            posts = [json.loads(line) for line in posts_path.read_text().splitlines() if line] if posts_path.exists() else []
            result = coverage(selected, report, posts, failed=bool(code))
        except (ValueError, TypeError, KeyError, AttributeError, OSError):
            code = code or 1
            result = coverage(selected, {}, [], failed=True)
            result['report_validation'] = 'FAILED'
        catalog_platforms = {}
        for source in catalog['sources']:
            stats = catalog_platforms.setdefault(source['platform'], {'configured': 0, 'enabled': 0})
            stats['configured'] += 1
            stats['enabled'] += source.get('enabled', True) is True
        result.update(catalog_platforms=catalog_platforms,
                      lane=args.lane, shard=args.shard_index, configured=len(catalog['sources']),
                      enabled=sum(s.get('enabled', True) is True for s in catalog['sources']))
        (output / 'coverage.json').write_text(json.dumps(result, indent=2))
        print(json.dumps(result))  # Aggregate counts and fixed status codes only.
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a', encoding='utf-8') as handle:
                handle.write('\n### Read-only pilot coverage\n\n```json\n' + json.dumps(result, indent=2) + '\n```\n')
        return code or (1 if result['unreported'] else 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', required=True)
    parser.add_argument('--lane', choices=LANES, required=True)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--shard-count', type=int, default=4)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        return execute(args)
    except Exception:
        print('PILOT_RUNTIME_FAILED', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
