"""Isolated collector-output to queue adapter; never performs network or archive writes.

This module supports *offline* E2E acceptance tests, not production delivery.
The trusted consumer hook must not be exposed over untrusted network inputs.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from envelope import SCHEMA, sign, validate


def candidate_from_collector(directory, *, workflow_run_id, attempt, lane, shard, created_at=None):
    """Build one bounded candidate envelope from an unprivileged collector output.

    Fail closed rather than silently discard >250 rows, invalid sources, or
    mismatched canonical keys. Never include catalog secrets or process env.
    """
    if type(workflow_run_id) is not int or workflow_run_id <= 0:
        raise ValueError('invalid_workflow_run_id')
    if type(attempt) is not int or not 1 <= attempt <= 99:
        raise ValueError('invalid_attempt')
    if lane not in ('fast', 'extended') or type(shard) is not int or not 0 <= shard < 4:
        raise ValueError('invalid_lane_or_shard')
    directory = Path(directory)
    report = json.loads((directory / 'report.json').read_text(encoding='utf-8'))
    if report.get('format') != 'argus-collection-report-v1':
        raise ValueError('invalid_collector_report')
    if report.get('shard_index') != shard or report.get('shard_count') != 4:
        raise ValueError('collector_shard_mismatch')
    reports = report.get('sources')
    if not isinstance(reports, list):
        raise ValueError('invalid_report_sources')
    posts = []
    with (directory / 'posts.jsonl').open(encoding='utf-8') as stream:
        for line in stream:
            if line.strip():
                posts.append(json.loads(line))
                if len(posts) > 250:
                    raise ValueError('batch_requires_partitioning')
    sources = []
    expected = set()
    for source in reports:
        keys = source.get('canonical_keys')
        if not isinstance(keys, list) or len(set(keys)) != len(keys):
            raise ValueError('invalid_source_keys')
        expected.update(keys)
        sources.append({k: source[k] for k in ('source_id', 'platform', 'status', 'posts')})
        if len(keys) != source['posts']:
            raise ValueError('per_source_count_mismatch')
    # Collector post set must equal the union of accepted per-source identity sets.
    # This catches incomplete output or mismatched report/package, including zero-post runs.
    actual = [p.get('canonical_key') for p in posts]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError('collector_posts_mismatch')
    origin = {s['source_id']: s['platform'] for s in sources}
    if len(origin) != len(sources):
        raise ValueError('duplicate_source_id')
    for p in posts:
        if origin.get(p.get('source_id')) != p.get('platform'):
            raise ValueError('post_source_platform_mismatch')
    batch = {
        'schema': SCHEMA,
        'run_id': f'{workflow_run_id}:{attempt}:{lane}:{shard}',
        'lane': lane,
        'shard': shard,
        'created_at': created_at or datetime.now(timezone.utc).isoformat(),
        'sources': sources,
        'posts': posts,
    }
    validate(batch)
    return batch


def signed_candidate(directory, *, key, **kwargs):
    payload = candidate_from_collector(directory, **kwargs)
    digest, signature = sign(payload, key)
    return payload, digest, signature


def drain_verified_once(queue, archive, *, now=None):
    """Test harness for queue contract and trusted readback protocol.

    archive must have import_candidate(payload) and readback_identity(run_id),
    where readback_identity returns an independently verified dict of exact
    {run_id, sha256, canonical_keys}. A real Hostless adapter is NOT supplied.
    No HTTP server or archive import implementation is included.
    """
    lease = queue.claim(lease_seconds=120, now=now)
    if lease is None:
        return None
    try:
        payload = json.loads(lease['body'])
        keys = sorted(p['canonical_key'] for p in payload['posts'])
        archive.import_candidate(payload, sha256=lease['sha256'])
        receipt = archive.readback_identity(payload['run_id'])
        if not isinstance(receipt, dict) or receipt != {
            'run_id': payload['run_id'],
            'sha256': lease['sha256'],
            'canonical_keys': keys,
        }:
            raise ValueError('independent_archive_readback_mismatch')
        queue.acknowledge(payload['run_id'], lease['lease_token'], archive_verified=True, now=now)
        return payload['run_id']
    except Exception:
        # If process crashes before release, lease naturally expires, so the
        # next trusted consumer retries. Archive import MUST be idempotent.
        try:
            queue.release(lease['run_id'], lease['lease_token'], now=now)
        except ValueError:
            pass
        raise
