"""Publish validated Argus packages to private Drive; commit index only after uploads."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'local-storage'))
from storage import validate_package, timestamp
from drive_store import from_environment, encoded, MAX_JSON, write_parts, read_parts, reuse_snapshot_parts
sys.path.append(str(Path(__file__).resolve().parents[1]/'collect'))
from social_links import validated_urls, profile_links
from source_origin import catalog_origins
from record_quality import merge_observations, video_caption
from url_identity import archive_identities, identity_keys

FORMAT = 'argus-drive-catalog-v1'
PARTITIONED_FORMAT = 'argus-drive-catalog-v2'

def now(): return dt.datetime.now(dt.timezone.utc).isoformat()

def load_snapshot(store, catalog=None):
    from feed_journal import replay
    return replay(store,load_base_snapshot(store,catalog))


def load_base_snapshot(store, catalog=None, *, item_consumer=None):
    if catalog is None:
        catalog = store.read_json('catalog.json')
    if not catalog: return {'items':[], 'packages':[], 'updated_at':None}
    if catalog.get('format') == PARTITIONED_FORMAT:
        count=0
        def consume(item):
            nonlocal count
            count+=1;item_consumer(item)
        snapshot=read_parts(store,catalog['snapshot_parts'],catalog['snapshot_sha256'],
                            item_consumer=consume if item_consumer is not None else None)
        if (count if item_consumer is not None else len(snapshot['items']))!=catalog['posts'] or len(snapshot['packages'])!=catalog['packages']:
            raise ValueError('Archive catalog count mismatch')
        return snapshot
    if catalog.get('format') != FORMAT: raise ValueError('Unsupported cloud catalog')
    import io
    from stream_json import load
    raw=store.read_bytes(catalog['snapshot_name'],MAX_JSON)
    if raw is None or hashlib.sha256(raw).hexdigest()!=catalog['snapshot_sha256']:
        raise ValueError('Stored snapshot integrity check failed')
    with io.TextIOWrapper(io.BytesIO(raw),encoding='utf-8') as text:
        return load(text,item_consumer=item_consumer)


def commit_snapshot(store, snapshot, raw=None, *, base=None, previous=None):
    if raw is None:
        from drive_store import encoded_file,write_file_parts
        with tempfile.TemporaryFile(mode='w+b') as spool:
            sha,size=encoded_file(snapshot,spool)
            catalog={'snapshot_sha256':sha,'updated_at':snapshot['updated_at'],
                     'posts':len(snapshot['items']),'packages':len(snapshot['packages'])}
            if size>MAX_JSON:
                catalog.update(format=PARTITIONED_FORMAT,snapshot_parts=write_file_parts(store,spool,size))
            else:
                spool.seek(0);name='snapshot-'+sha+'.json'
                catalog.update(format=FORMAT,snapshot_name=name,snapshot_id=store.put(name,spool.read()))
            if base is not None and store.read_json('catalog.json')!=base:
                raise ValueError('Archive changed during import; retry collected package')
            store.json('catalog.json',catalog,mutable=True)
        return
    sha = hashlib.sha256(raw).hexdigest()
    catalog = {'snapshot_sha256':sha,'updated_at':snapshot['updated_at'],
               'posts':len(snapshot['items']),'packages':len(snapshot['packages'])}
    if len(raw)>MAX_JSON:
        if base and base.get('format') == PARTITIONED_FORMAT and previous is not None:
            parts = reuse_snapshot_parts(store, raw, encoded(previous), base['snapshot_parts'])
        else:
            parts = write_parts(store,raw)
        catalog.update(format=PARTITIONED_FORMAT,snapshot_parts=parts)
    else:
        name = 'snapshot-'+sha+'.json'
        catalog.update(format=FORMAT,snapshot_name=name,snapshot_id=store.put(name,raw))
    if base is not None and store.read_json('catalog.json') != base:
        raise ValueError('Archive changed during import; retry collected package')
    store.json('catalog.json',catalog,mutable=True)

def publish(store, archive, allow_sample=False, *, measure=False, changes_only=False,
            previous_snapshot=None,commit_callback=None,import_time=None):
    imported=timestamp(import_time) if import_time is not None else now()
    store.assert_private()
    base = store.read_json('catalog.json')
    previous = previous_snapshot if previous_snapshot is not None else load_snapshot(store, base)
    with tempfile.TemporaryDirectory() as temp:
        with validate_package(archive,temp) as (manifest, posts, extracted):
            if manifest['sample'] and not allow_sample:
                raise ValueError('Sample package requires explicit opt-in')
            origins = catalog_origins(store,register=not manifest['sample'])
            package_id = manifest['package_id']
            if package_id in previous['packages']:
                return {'status':'already_imported','posts':len(previous['items']),
                        'new_keys':previous.get('collection_deltas', {}).get(package_id)}
            observations = posts
            if changes_only:
                from publish_hourly import changes
                observations, _ = changes(posts, previous)
                if not observations:
                    return {'status':'unchanged','posts':len(previous['items']),'new_keys':[]}
            media = {}
            for entry in manifest['files']:
                if entry['path'].startswith('media/'):
                    sha = entry['sha256']
                    file_id = store.put('media-'+sha,(extracted/entry['path']).read_bytes())
                    media[sha] = {'sha256':sha,'drive_id':file_id,'size':entry['size']}
            items = {item['id']:item for item in previous['items']}
            if len(items)!=len(previous['items']):
                raise ValueError('Repeated ID in previous archive')
            aliases = archive_identities(previous['items'])
            new_keys = []
            for post in observations:
                keys = identity_keys(post)
                archive_id = next((aliases[k] for k in keys if k in aliases), keys[0])
                old = items.get(archive_id)
                for k in keys:
                    aliases.setdefault(k,archive_id)
                if not old:
                    new_keys.append(archive_id)
                names = {entry.get('sha256'):str(entry.get('name','')) for entry in post.get('evidence_files',[]) if isinstance(entry,dict)}
                files = [{**media[sha],'name':Path(names.get(sha) or sha).name} for sha in post['media']]
                items[archive_id] = {
                    'id':archive_id,'platform':post['platform'],'url':post['post_url'],
                    'text':post['text'],'published_at':post['published_at'],'observed_at':post['observed_at'],
                    'discovered_at':timestamp(post.get('discovered_at') or post['observed_at']),
                    'collected_at':timestamp(post.get('collected_at') or post['observed_at']),
                    'imported_at':imported,'sample':manifest['sample'],'tool':post.get('tool') or post.get('capture_mode',''), 'files':files,
                    'source_id':post.get('source_id',''),'source_url':post.get('source_url',''),
                    'source_name':str(post.get('source_name',''))[:1000],
                    'outbound_social_links':validated_urls(post.get('outbound_social_links',[])),
                    'outbound_profile_links':profile_links(post.get('outbound_profile_links',[])),
                    'text_may_be_truncated':post.get('text_may_be_truncated'),
                    'media_urls':[u for u in post.get('media_urls',[]) if isinstance(u,str) and u.startswith(('https://','http://'))][:12]}
                for field in ('source_match','capture_mode','publisher_embed_url','gdelt_seen_at'):
                    if post.get(field):
                        items[archive_id][field] = post[field]
                # Keep verified API identities across archive normalization and
                # subsequent observations which omit optional metadata.
                for field in ('platform_post_id','conversation_id'):
                    value=post.get(field) or (old or {}).get(field)
                    if isinstance(value,str) and re.fullmatch(r'\d{1,30}(?:_\d{1,30})?',value):
                        items[archive_id][field]=value
                if post['platform']=='x' and 'x_references' in post:
                    items[archive_id]['x_references'] = post['x_references']
                if post['platform']=='youtube' and isinstance(post.get('public_date_evidence'),dict):
                    items[archive_id]['public_date_evidence']=post['public_date_evidence']
                if post['platform'] == 'instagram':
                    comments = post.get('instagram_comments') or (old or {}).get('instagram_comments')
                    if isinstance(comments, list) and comments:
                        items[archive_id]['instagram_comments'] = comments[:20]
                if post['platform'] == 'tiktok':
                    saved = items[archive_id]
                    analysis_text = post.get('video_analysis_text') or (old or {}).get('video_analysis_text')
                    if isinstance(analysis_text, str) and analysis_text.strip():
                        saved['video_analysis_text'] = analysis_text[:20000]
                        saved['text'] = video_caption(saved['text'], saved['video_analysis_text'])
                if post['platform']=='facebook':
                    saved=items[archive_id]
                    if post.get('public_comments'):
                        saved['public_comments']=[{'id':c['id'][:200],'text':c['text'][:4000]}
                            for c in post['public_comments'][:20] if isinstance(c,dict) and
                            isinstance(c.get('id'),str) and isinstance(c.get('text'),str)]
                    for field in ('facebook_text_source','facebook_date_source','facebook_story_id'):
                        if post.get(field):
                            saved[field]=post[field]
                    if old and old.get('published_at') and not saved['published_at']:
                        saved['published_at']=old['published_at']
                        saved['facebook_date_source']='archived_publication_date'
                    if old and old.get('facebook_text_source') in ('story_message','expanded_card') and saved['text_may_be_truncated']:
                        teaser=re.sub(r'(?:…|\.\.\.)\s*(?:see more|більше|показати більше)?\s*$','',saved['text'],flags=re.I).strip()
                        if teaser and old['text'].startswith(teaser):
                            saved.update(text=old['text'],text_may_be_truncated=False,facebook_text_source=old['facebook_text_source'],facebook_retained_text=True)
                # Preserve optional publisher metrics and metadata; missing is unknown.
                for field in ('title','category','language','keywords','engagement','likes','views','comments_count','shares','region'):
                    if field in post: items[archive_id][field]=post[field]
                merged = merge_observations(old, items[archive_id])
                from analytics_snapshot import normalize
                merged=normalize(merged,origins.metadata.get((merged['platform'],merged.get('source_id'))))
                if old and old.get('public_date_evidence') and old.get('published_at')==merged.get('published_at'):
                    merged.setdefault('public_date_evidence',old['public_date_evidence'])
                merged['origin'] = origins.resolve(merged)
                merged['quality_flags'] = (['publication_date_unknown'] if not merged.get('published_at') else []) + (
                    ['media_only'] if not merged.get('text') and (merged.get('media_urls') or merged.get('files')) else [])
                if (old and merged['origin'] == 'unknown' and not merged.get('source_id')
                        and not merged.get('source_url') and not merged.get('source_match')
                        and merged.get('capture_mode') != 'search_preview'):
                    merged['origin'] = old.get('origin', 'unknown')
                linked = set((old or {}).get('identity_aliases',[])).union(keys)-{archive_id}
                if linked: merged['identity_aliases'] = sorted(linked)
                merged['undated'] = merged.get('published_at') is None
                merged['first_archived_at'] = (old.get('first_archived_at') if old else merged['imported_at'])
                # Existing snapshots only guarantee the earliest *retained* observation.
                if old and (not old.get('discovered_at') or old.get('discovery_is_upper_bound')):
                    merged['discovery_is_upper_bound'] = True
                changed = not old or any(old.get(f) != merged.get(f) for f in
                    ('text','published_at','url','files','media_urls','origin','public_comments',
                     'instagram_comments','outbound_social_links','outbound_profile_links','text_may_be_truncated','video_analysis_text'))
                merged['updated_at'] = imported if changed else old.get('updated_at') or old.get('imported_at') or old['observed_at']
                items[archive_id] = merged
            # The archive view is ordered by first retained discovery. Refetching
            # old material must not reorder unchanged records and their chunks.
            snapshot = {'items':sorted(items.values(),key=lambda p:(p.get('discovered_at') or p['observed_at'],p['id']),reverse=True),
                        'packages':previous['packages']+[package_id],'updated_at':imported}
            if previous.get('live_checkpoint'):snapshot['live_checkpoint']=previous['live_checkpoint']
            # Couple insertion deltas to the same atomic catalog pointer as the posts.
            deltas = previous.get('collection_deltas', {}).copy()
            if measure:
                deltas[package_id] = new_keys
            if deltas:
                recent = [p for p in snapshot['packages'] if p in deltas][-8:]
                snapshot['collection_deltas'] = {p:deltas[p] for p in recent}
            # Keep original validated publication metadata, including historical versions.
            store.json('package-'+package_id+'.json', {'manifest':manifest,'posts':posts})
            if commit_callback:commit_callback(snapshot,previous,base)
            else:commit_snapshot(store, snapshot, base=base, previous=previous)
            return {'status':'imported','posts':len(items),'packages':len(snapshot['packages']), 'new_keys':new_keys}

def monitor(store, results, state):
    from json import loads
    stages=[]
    for tool in ('yt-dlp','archivebox','sherlock'):
        path=Path(results)/tool/'result.json'
        if path.is_file():
            report=loads(path.read_text(encoding='utf-8'))
            # No raw logs, source URLs, usernames, tokens, or environment dumps.
            metrics=[]
            for name in ('install','init','capture','decode-check'):
                values=report.get(name,{})
                clean={key:values[key] for key in ('wall_seconds','max_process_rss_kib')
                       if type(values.get(key)) in (int,float) and values[key]>=0}
                if clean: metrics.append({'stage':name,**clean})
            stages.append({'tool':tool,'status':report.get('status','unknown'),
                           'observed_at':report.get('observed_at'),'metrics':metrics})
    store.assert_private()
    previous=store.read_json('monitor.json') or {}
    return store.json('monitor.json',{**{k:previous[k] for k in ('source_health','incremental','history','focused_collection') if k in previous},
        'format':'argus-monitor-v1','updated_at':now(),
        'state':state,'stages':stages,'run_id':os.environ.get('GITHUB_RUN_ID',''),
        'repository':'cutebigmonkey-team/ArgusPRO'},mutable=True)

def confirm_collection(snapshot, result, report):
    if result['new_keys'] is None:
        raise ValueError('archive_delta_unavailable')
    inserted = set(result['new_keys'])
    report['new_to_archive'] = len(inserted)
    aliases = archive_identities(snapshot['items'])
    index = {p['id']:p for p in snapshot['items']}
    verified_at = dt.datetime.now(dt.timezone.utc)
    report['archive_verified_at']=verified_at.isoformat()
    report['platform_insertions']={}
    for sid in inserted:
        item=index[sid]
        # A retry may start hours after normalization. Only readback attests
        # durable visibility; first_archived_at is legacy import-attempt metadata.
        report['platform_insertions'].setdefault(item['platform'],{})[sid]=verified_at.isoformat()
    for row in report['sources']:
        row['new_keys'] = [k for k in row['canonical_keys'] if aliases[k] in inserted]
        saved_ids = {aliases[k] for k in row['canonical_keys']}
        telegram_pages = [page for attempt in row.get('attempts', []) if isinstance(attempt.get('pages'), list) for page in attempt['pages']
                          if isinstance(page, dict) and 'raw_rows' in page]
        row['delivery'] = {
            # Some adapters expose normalized objects only; never fabricate their input counts.
            'discovered_cards':sum(page['raw_rows'] for page in telegram_pages) if telegram_pages else None,
            'fetched_cards':row.get('parser_inputs') or None,
            'parsed_rows':row.get('pre_window_rows'),
            'normalized_keys':len(set(row['canonical_keys'])),
            'deduplicated_ids':len(saved_ids), 'saved_ids':len(saved_ids),
            'previously_saved_ids':len(saved_ids - inserted),
            'inserted_ids':len(saved_ids & inserted),
            'visible_ids':None,  # Only a consumer of the frontend API can attest visibility.
        }
        delays = []
        archive_delays = []
        for key in {aliases[k] for k in row['new_keys']}:
            item = index[key]
            discovered = dt.datetime.fromisoformat(item.get('discovered_at') or item['observed_at'])
            archive_delays.append((verified_at - discovered).total_seconds())
            if item.get('published_at') and not item.get('published_at_is_estimated'):
                lag = (discovered - dt.datetime.fromisoformat(item['published_at'])).total_seconds()
                if lag >= 0: delays.append(lag)
        row['collection_latency'] = {'unit':'seconds','source_to_discovery':delays,
            'discovery_to_archive_readback':archive_delays}


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':
        raise RuntimeError('Cloud publication runs only on GitHub-hosted Actions')
    parser=argparse.ArgumentParser()
    parser.add_argument('--package')
    parser.add_argument('--allow-sample',action='store_true')
    parser.add_argument('--verify-replay',action='store_true')
    parser.add_argument('--changes-only',action='store_true')
    parser.add_argument('--collection-report')
    parser.add_argument('--monitor')
    parser.add_argument('--state',choices=('passed','failed'),default='passed')
    args=parser.parse_args()
    store=from_environment()
    if args.package:
        result=publish(store,args.package,args.allow_sample,measure=bool(args.collection_report),changes_only=args.changes_only)
        print('Drive archive:',result['status'],'; posts:',result['posts'])
        if args.verify_replay and result['status'] != 'unchanged':
            catalog=store.read_json('catalog.json')
            replay=publish(store,args.package,args.allow_sample)
            if replay['status']!='already_imported' or replay['posts']!=result['posts'] or store.read_json('catalog.json')!=catalog:
                raise ValueError('Replay changed the archive')
            print('Replay verified: same package, unchanged catalog and post count.')
        if args.collection_report:
            path = Path(args.collection_report)
            report = json.loads(path.read_text(encoding='utf-8'))
            confirm_collection(load_snapshot(store), result, report)
            path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    if args.monitor: monitor(store,args.monitor,args.state)

if __name__=='__main__':
    try: main()
    except Exception as error:
        # Google exceptions can include request URLs. Never log raw exception content.
        print('Drive publication failed ('+type(error).__name__+'). Check configuration, permissions and quota.',file=sys.stderr)
        sys.exit(1)
