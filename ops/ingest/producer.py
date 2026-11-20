"""Prepare private bounded parts; delivery requires explicit trusted callbacks.

No OIDC credentials, HTTP client, archive writes or checkpoint commit implementation.
Original collector files are never changed or removed. Prepared files are private,
not GitHub artifacts. A disposable runner is NOT durable storage.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from envelope import canonical, _iso
from partition import from_collector, decode, MAX_RECORDS


def prepare(directory, output, **identity):
    root=Path(directory);target=Path(output)
    if target.exists():raise ValueError('output_must_not_exist')
    with (root/'report.json').open('rb') as file:raw_report=file.read(8_000_001)
    if len(raw_report)>8_000_000:raise ValueError('report_too_large')
    report=json.loads(raw_report);rows=[];total_bytes=0
    with (root/'posts.jsonl').open(encoding='utf-8') as stream:
        while True:
            line=stream.readline(2_000_001)
            if not line:break
            total_bytes+=len(line.encode('utf-8'))
            if len(line)>2_000_000 or total_bytes>64_000_000:raise ValueError('producer_output_too_large')
            if line.strip():rows.append(json.loads(line))
            if len(rows)>MAX_RECORDS:raise ValueError('producer_requires_continuation')
    packets=from_collector(report,rows,**identity)
    target.mkdir(mode=0o700,parents=True)
    index=[]
    for packet in packets:
        body=canonical(packet);name=f"{packet['group_id']}-{packet['index']}.json"
        with (target/name).open('xb') as file:
            os.chmod(target/name,0o600);file.write(body);file.flush();os.fsync(file.fileno())
        index.append({'file':name,'sha256':hashlib.sha256(body).hexdigest()})
    with (target/'plan.json').open('xb') as file:
        os.chmod(target/'plan.json',0o600);file.write(canonical({'schema':'argus-producer-plan-v2','parts':index}))
        file.flush();os.fsync(file.fileno())
    fd=os.open(target,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return {'parts':len(packets),'records':len(rows),'archive_verified':False,'checkpoint_advanced':False}


def submit_prepared(directory, oidc_token_provider, send_request, *, enabled=False):
    if not enabled:raise ValueError('production_delivery_disabled')
    root=Path(directory);plan=json.loads((root/'plan.json').read_bytes())
    if set(plan)!={'schema','parts'} or plan['schema']!='argus-producer-plan-v2':raise ValueError('invalid_producer_plan')
    result=[]
    for entry in plan['parts']:
        name=entry['file']
        if Path(name).name!=name or not name.endswith('.json'):raise ValueError('invalid_part_path')
        body=(root/name).read_bytes()
        if hashlib.sha256(body).hexdigest()!=entry['sha256']:raise ValueError('prepared_part_corrupt')
        value=json.loads(body);decode(body,now=_iso(value['created_at']))
        # Mint a fresh short-lived identity per batch. Retry may mint a new jti;
        # deterministic batch identity still makes submissions idempotent.
        receipt=send_request(body,oidc_token_provider())
        if receipt not in ('queued','already_queued','already_acknowledged'):
            raise ValueError('queue_receipt_required')
        result.append(receipt)
    # No unlink, archive acknowledgement or source cursor advancement here.
    return result



def restore_for_collector(approved_sources, fetch_verified_cursor, output, *, enabled=False):
    """Private, authenticated recovery callback; produces run.py-compatible input.

    No HTTP client is provided. Caller must use the reviewed private gateway and
    verify TLS. Restored files are NEVER public Actions artifacts or log output.
    """
    import re
    if not enabled:raise ValueError('production_cursor_recovery_disabled')
    target=Path(output)
    if target.exists():raise ValueError('output_must_not_exist')
    cursors={};revisions={}
    for source in approved_sources:
        sid=source['id']
        if sid in revisions:raise ValueError('duplicate_source_id')
        value=fetch_verified_cursor(sid)
        if not isinstance(value,dict) or set(value)!={'source_id','revision','checkpoint','verified_group'} or value['source_id']!=sid:
            raise ValueError('unverified_cursor_identity')
        revision=value['revision'];checkpoint=value['checkpoint'];group=value['verified_group']
        if type(revision) is not int or revision<0 or (revision>0 and (not isinstance(group,str) or not re.fullmatch('[0-9a-f]{64}',group))):
            raise ValueError('unverified_cursor_revision')
        if checkpoint is not None:
            if not isinstance(checkpoint,dict) or checkpoint.get('url')!=source['url']:
                raise ValueError('cursor_source_url_mismatch')
            cursors[sid]=checkpoint
        revisions[sid]=revision
    target.mkdir(mode=0o700,parents=True)
    state={'schema':'argus-collection-cursors-v1','incremental_sources':cursors,'incremental_polling':{}}
    for name,value in (('cursors.json',state),('revisions.json',{'schema':'argus-ingest-revisions-v1','revisions':revisions})):
        with (target/name).open('xb') as file:
            os.chmod(target/name,0o600);file.write(canonical(value));file.flush();os.fsync(file.fileno())
    return {'sources':len(revisions),'restored':len(cursors),'checkpoint_advanced':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--collector-output',required=True);p.add_argument('--output',required=True)
    p.add_argument('--run-id',type=int,required=True);p.add_argument('--attempt',type=int,required=True)
    p.add_argument('--lane',choices=('fast','extended'),required=True);p.add_argument('--shard',type=int,required=True)
    p.add_argument('--sha',required=True);p.add_argument('--revisions')
    a=p.parse_args()
    try:
        revisions=None
        if a.revisions:
            loaded=json.loads(Path(a.revisions).read_bytes())
            if set(loaded)!={'schema','revisions'} or loaded['schema']!='argus-ingest-revisions-v1':raise ValueError('invalid_revisions')
            revisions=loaded['revisions']
        print(json.dumps(prepare(a.collector_output,a.output,run_id=a.run_id,attempt=a.attempt,lane=a.lane,shard=a.shard,sha=a.sha,revisions=revisions)))
    except Exception:raise SystemExit('PRODUCER_PREPARATION_FAILED') from None
