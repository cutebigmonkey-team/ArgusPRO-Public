"""Deterministic bounded per-source envelopes. No delivery, credentials or cursors writes."""
import hashlib
import json
import re
from collections import Counter
from envelope import canonical, validate as validate_v1, SCHEMA as V1, MAX_BYTES, MAX_POSTS, _reject_duplicates, _iso

SCHEMA = 'argus-ingest-part-v2'
MAX_PARTS = 256
MAX_RECORDS = 10000
SAFE_CHECKPOINT = {'ok','no_new_posts','no_public_posts','partial_results','partial_telegram_cards'}


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def manifest(part):
    return {k: part[k] for k in ('producer','source','checkpoint','created_at','parts','record_count')}


def validate_part(part, *, now=None):
    if not isinstance(part,dict) or set(part) != {'schema','group_id','producer','source','checkpoint','created_at','parts','record_count','index','posts'}:
        raise ValueError('invalid_part_shape')
    if part['schema'] != SCHEMA or part['group_id'] != digest(manifest(part)):
        raise ValueError('invalid_group_digest')
    producer=part['producer']
    if not isinstance(producer,dict) or set(producer) != {'run_id','attempt','lane','shard','sha'}:
        raise ValueError('invalid_producer')
    if not isinstance(producer['sha'],str) or not re.fullmatch('[0-9a-f]{40}',producer['sha']):
        raise ValueError('invalid_producer_sha')
    if type(producer['run_id']) is not int or producer['run_id']<=0 or type(producer['attempt']) is not int or not 1<=producer['attempt']<=99:
        raise ValueError('invalid_producer_identity')
    parts=part['parts'];index=part['index'];posts=part['posts']
    if not isinstance(parts,list) or not 1<=len(parts)<=MAX_PARTS or any(not isinstance(p,str) or not re.fullmatch('[0-9a-f]{64}',p) for p in parts):
        raise ValueError('invalid_part_manifest')
    if type(index) is not int or not 0<=index<len(parts) or not isinstance(posts,list) or digest(posts)!=parts[index]:
        raise ValueError('part_digest_mismatch')
    if type(part['record_count']) is not int or not len(posts)<=part['record_count']<=MAX_RECORDS:
        raise ValueError('invalid_record_count')
    if (part['record_count']==0 and len(parts)!=1) or (part['record_count']>0 and (not posts or not len(parts)<=part['record_count']<=len(parts)*MAX_POSTS)):
        raise ValueError('invalid_record_count')
    source=part['source']; checkpoint=part['checkpoint']
    if not isinstance(source,dict) or set(source) != {'source_id','platform','status'}:
        raise ValueError('invalid_source')
    if not isinstance(checkpoint,dict) or set(checkpoint) != {'base_revision','proposal'} or type(checkpoint['base_revision']) is not int or not 0<=checkpoint['base_revision']<2**63-1:
        raise ValueError('invalid_checkpoint')
    if checkpoint['proposal'] is not None and (not isinstance(checkpoint['proposal'],dict) or source['status'] not in SAFE_CHECKPOINT):
        raise ValueError('unsafe_checkpoint')
    if checkpoint['proposal'] is not None and source['status'] in ('partial_results','partial_telegram_cards') and part['record_count']==0:
        raise ValueError('incomplete_empty_checkpoint')
    if len(canonical(checkpoint))>65536:
        raise ValueError('checkpoint_too_large')
    envelope={'schema':V1,'run_id':f"{producer['run_id']}:{producer['attempt']}:{producer['lane']}:{producer['shard']}",
              'lane':producer['lane'],'shard':producer['shard'],'created_at':part['created_at'],
              'sources':[{**source,'posts':len(posts)}],'posts':posts}
    validate_v1(envelope,now=now)
    from storage import validate_post
    for post in posts:
        try:
            validate_post(post)  # The real package contract, with no media inventory.
        except (ValueError, TypeError, OverflowError):
            raise ValueError('archive_post_contract_invalid') from None
        if any(not isinstance(post.get(k),str) or '\0' in post[k] for k in ('canonical_key','platform','source_id','post_url','text','observed_at')):
            raise ValueError('invalid_post_fields')
        _iso(post['observed_at'])
        if len(post['text'])>100000 or post.get('media'):
            raise ValueError('unsupported_post_payload')
        from urllib.parse import urlsplit
        if urlsplit(post['post_url']).scheme not in ('https','http'):raise ValueError('invalid_post_url')
    if any(p['source_id']!=source['source_id'] or p['platform']!=source['platform'] or not p['canonical_key'].startswith(source['platform']+':') for p in posts):
        raise ValueError('post_provenance_mismatch')
    body=canonical(part)
    if len(body)>MAX_BYTES:raise ValueError('payload_too_large')
    return body


def decode(body, *, now=None):
    if not isinstance(body,bytes) or len(body)>MAX_BYTES:raise ValueError('oversized_body')
    try:
        part=json.loads(body,object_pairs_hook=_reject_duplicates,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_number')))
        if validate_part(part,now=now)!=body:raise ValueError('noncanonical_part')
    except (UnicodeError,RecursionError,TypeError,KeyError) as error:
        raise ValueError('invalid_part') from error
    return part


def partition_source(producer,source,posts,checkpoint,created_at, *, now=None):
    if len(posts)>MAX_RECORDS:raise ValueError('source_requires_continuation')
    ordered=sorted(posts,key=lambda p:p['canonical_key'])
    if len({p['canonical_key'] for p in ordered})!=len(ordered):raise ValueError('duplicate_identity')
    chunks=[ordered[i:i+MAX_POSTS] for i in range(0,len(ordered),MAX_POSTS)] or [[]]
    while True:
        base={'producer':producer,'source':source,'checkpoint':checkpoint,'created_at':created_at,
              'parts':[digest(c) for c in chunks],'record_count':len(posts)}
        group=digest(base);result=[];split=None
        if len(chunks)>MAX_PARTS:raise ValueError('too_many_parts')
        for index,chunk in enumerate(chunks):
            part={'schema':SCHEMA,'group_id':group,**base,'index':index,'posts':chunk}
            try:validate_part(part,now=now)
            except ValueError as error:
                if str(error)=='payload_too_large' and len(chunk)>1:
                    split=index;break
                raise
            result.append(part)
        if split is None:return result
        chunk=chunks[split];half=len(chunk)//2
        chunks[split:split+1]=[chunk[:half],chunk[half:]]


def from_collector(report,posts, *, run_id,attempt,lane,shard,sha,revisions=None,now=None):
    if report.get('format')!='argus-collection-report-v1' or report.get('shard_index')!=shard or report.get('shard_count')!=4:
        raise ValueError('collector_report_mismatch')
    sources=report['sources'];by_id={s['source_id']:s for s in sources}
    if len(by_id)!=len(sources):raise ValueError('duplicate_source_id')
    actual=Counter();seen=set();rows={sid:[] for sid in by_id}
    for post in posts:
        sid=post['source_id'];key=post['canonical_key']
        if sid not in rows or key in seen:raise ValueError('collector_posts_mismatch')
        seen.add(key);actual[sid]+=1;rows[sid].append(post)
    result=[]
    for sid,source in sorted(by_id.items()):
        keys=source['canonical_keys']
        if len(keys)!=len(set(keys)) or set(keys)!={p['canonical_key'] for p in rows[sid]} or source['posts']!=actual[sid]:
            raise ValueError('collector_source_count_mismatch')
        proposal=report.get('state_updates',{}).get(sid) if source['status'] in SAFE_CHECKPOINT else None
        if source['status'] in ('partial_results','partial_telegram_cards') and not rows[sid]:proposal=None
        result.extend(partition_source({'run_id':run_id,'attempt':attempt,'lane':lane,'shard':shard,'sha':sha},
            {k:source[k] for k in ('source_id','platform','status')},rows[sid],
            {'base_revision':(revisions or {}).get(sid,0),'proposal':proposal},report['updated_at'],now=now))
    return result
