"""Recover a staged microbatch under the existing Drive writer lock before admitting another."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile

from collection_state import commit
from drive_store import from_environment, encoded
from publish import publish, load_snapshot, confirm_collection
from storage import validate_package

PENDING = 'incremental-pending.json'


def deliver(store, package, report, *, before_readback=None):
    result = publish(store,package,measure=True,changes_only=True)
    # Publication releases its snapshot and encoded buffers before this point.
    # Small cloud workers can return retained allocator pages before decoding
    # the independent verification snapshot. Never skip that verification.
    if before_readback is not None:
        before_readback()
    snapshot = load_snapshot(store)
    confirm_collection(snapshot,result,report)
    if not report.get('permalink_backfill'):
        commit(store,report,snapshot=snapshot)
    write_monitor(store,report,snapshot)


def write_monitor(store,report,snapshot,*,verified_state=None,catalog=None):
    from datetime import datetime,timezone
    from collection_health import summarize
    from monitoring_metrics import summarize as monitoring_summary
    monitor=store.read_json('monitor.json') or {'format':'argus-monitor-v1'}
    lane='history' if report.get('backfill') else 'incremental' if report.get('incremental') else 'focused_collection'
    first=next(iter(report.get('started_sources',[])),None)
    monitor[lane]={'run_id':report['run_id'],'checked_at':report['updated_at'],'first_started':first,
        'first_news':report.get('news_signals',{}).get(first),'selected':len(report['sources']),
        'new_to_archive':report['new_to_archive'],'archive_verified':sum(row.get('archived_posts',0) for row in report['sources'])}
    if catalog is None:catalog=store.read_json('sources.json') or {}
    state=verified_state if verified_state is not None else store.read_json('collection-cursors.json') or {}
    when=datetime.now(timezone.utc)
    monitor['source_health']=summarize(catalog,state,snapshot['items'],when)
    monitor['coverage']=monitoring_summary(catalog,state,snapshot['items'],when)
    store.json('monitor.json',monitor,mutable=True)
    return monitor


def drain(store, package, report, *, before_readback=None, journal=None):
    blob = package.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    # Reject malformed packages before they can occupy the single recovery slot.
    with tempfile.TemporaryDirectory() as temp, validate_package(package,temp) as (manifest, posts, _):
        if manifest['sample'] or {p['canonical_key'] for p in posts} != {k for r in report['sources'] for k in r['canonical_keys']}:
            raise ValueError('incremental_batch_manifest_mismatch')
    # PostgreSQL retains this exact package in Hostless. Journal pages are the
    # durable Drive archive; do not duplicate its recovery blob on every tick.
    if journal:
        journal.deliver(package,report)
        return
    staged = store.read_json(PENDING) or {}
    if staged.get('state')=='pending':
        if staged['sha256'] != digest:
            prior = base64.b64decode(staged['package'],validate=True)
            if hashlib.sha256(prior).hexdigest() != staged['sha256']:
                raise ValueError('staged_batch_checksum_mismatch')
            with tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'package.zip'
                path.write_bytes(prior)
                deliver(store,path,staged['report'],before_readback=before_readback)
    if staged.get('sha256') != digest or staged.get('state') != 'pending':
        store.json(PENDING,{'state':'pending','sha256':digest,'package':base64.b64encode(blob).decode(),
                            'report':report},mutable=True)
    deliver(store,package,report,before_readback=before_readback)
    store.json(PENDING,{'state':'acknowledged','sha256':digest,'run_id':report['run_id']},mutable=True)


if __name__=='__main__':
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise SystemExit('Hosted writer required')
    directory = Path('batch')
    report_path = directory/'report.json'
    report = json.loads(report_path.read_bytes())
    try:
        store = from_environment()
        store.assert_private()
        from site_sources import with_submissions
        catalog = store.read_json('sources.json')
        if catalog is not None:
            updated = with_submissions(store, catalog)
            if updated != catalog:
                store.json('sources.json', updated, mutable=True)
                if store.read_json('sources.json') != updated:
                    raise ValueError('Website source registry readback failed')
        drain(store,directory/'package.zip',report)
    except Exception as exc:
        report['archive_status'] = 'Archive Error'
        report_path.write_bytes(encoded(report))
        raise SystemExit('Incremental publication failed: '+type(exc).__name__)
    report['archive_status'] = 'verified'
    report_path.write_bytes(encoded(report))
