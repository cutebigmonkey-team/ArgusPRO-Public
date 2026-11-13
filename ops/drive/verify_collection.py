"""Read back the private archive and compare it with a completed cloud run."""
import argparse
import collections
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit, parse_qs

from drive_store import DriveStore, from_environment
from publish import load_snapshot, now
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'local-storage'))
from storage import validate_package, timestamp


def unique_items(items):
    ids = [p['id'] for p in items]
    if len(ids) != len(set(ids)): raise ValueError('duplicate_canonical_keys')
    identities = []
    for item in items:
        if item.get('sample'): continue
        url = urlsplit(item['url']); platform = item['platform']
        identity = None
        if platform == 'youtube' and url.hostname in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
            video = parse_qs(url.query).get('v', [''])[0] if url.path == '/watch' else url.path.removeprefix('/shorts/')
            if re.fullmatch(r'[A-Za-z0-9_-]{11}', video): identity = 'youtube:'+video
        elif platform == 'telegram' and url.hostname == 't.me':
            path = url.path.strip('/').removeprefix('s/')
            if re.fullmatch(r'[A-Za-z0-9_]+/\d+', path): identity = 'telegram:'+path.lower()
        if identity: identities.append(identity)
    if len(identities) != len(set(identities)): raise ValueError('duplicate_platform_identities')
    return {p['id']:p for p in items}


def verify_posts(expected, snapshot):
    from url_identity import archive_identities
    from record_quality import video_caption
    archived = unique_items(snapshot['items']); exact = 0; newer = 0; retained = 0
    aliases = archive_identities(snapshot['items'])
    for key, post in expected.items():
        item = archived.get(aliases.get(key,key))
        if not item or item.get('sample'): raise ValueError('collected_post_missing_from_archive')
        if item['platform'] != post['platform']: raise ValueError('archive_platform_mismatch')
        old, current = timestamp(post['observed_at']), timestamp(item['observed_at'])
        if current < old: raise ValueError('archive_post_older_than_collection')
        if current == old:
            expected_date=post.get('published_at')
            expected_text=post.get('text')
            if item.get('retained_date_observed_at') and expected_date is None:
                expected_date=item.get('published_at')
            if item.get('retained_text_observed_at') and (not expected_text or post.get('text_may_be_truncated') is True):
                expected_text=item['text']
            if post['platform']=='facebook':
                if expected_date is None and item.get('facebook_date_source')=='archived_publication_date':
                    expected_date=item['published_at']
                if post.get('text_may_be_truncated') and item.get('facebook_retained_text') and item['text'].startswith(re.split(r'(?:…|\.\.\.)',expected_text)[0].strip()):
                    expected_text=item['text']
            if post['platform']=='tiktok' and item.get('text')!=expected_text:
                expected_text=video_caption(expected_text,item.get('video_analysis_text'))
            if item.get('url')!=post.get('post_url') or item.get('text')!=expected_text or item.get('published_at')!=expected_date:
                raise ValueError('archive_content_mismatch')
            if expected_text==post.get('text') and expected_date==post.get('published_at'):
                exact += 1
            else:
                retained += 1
        else: newer += 1
    return {'expected_posts':len(expected), 'exact_matches':exact, 'newer_versions':newer,'retained_richer_records':retained,
            'archive_posts':len(archived), 'archive_real_posts':sum(not p.get('sample') for p in archived.values()),
            'duplicate_canonical_keys':0, 'duplicate_youtube_telegram_identities':0}


def read_parts(directory, run_id, catalog_path, registry_path):
    catalog_bytes = Path(catalog_path).read_bytes()
    catalog = json.loads(catalog_bytes)
    digest = hashlib.sha256(catalog_bytes).hexdigest()
    registry_digest = hashlib.sha256(Path(registry_path).read_bytes()).hexdigest()
    expected_sources = {s['id'] for s in catalog['sources'] if s.get('enabled', True)}
    results = []; posts = {}; seen = set(); memberships = collections.defaultdict(set)
    for index in range(12):
        part = Path(directory)/('argus-platform-part-'+str(index))
        report = json.loads((part/'report.json').read_text(encoding='utf-8'))
        if (report['run_id'] != run_id or report['shard_index'] != index or report['shard_count'] != 12
                or report['catalog_sha256'] != digest or report.get('registry_sha256') != registry_digest):
            raise ValueError('unexpected_collection_artifact')
        for result in report['sources']:
            identity = result['source_id']
            if identity in seen or identity not in expected_sources: raise ValueError('unexpected_source_result')
            seen.add(identity); results.append(result)
        with tempfile.TemporaryDirectory() as temp:
            with validate_package(part/'package.zip', temp) as (manifest, items, _):
                if manifest['sample']: raise ValueError('sample_collection_artifact')
                for post in items:
                    key = post['canonical_key']
                    memberships[post['source_id']].add(key)
                    if key not in posts or post['observed_at'] > posts[key]['observed_at']: posts[key] = post
    if seen != expected_sources: raise ValueError('incomplete_source_collection')
    return catalog, results, posts, memberships


def agency_report(config, catalog, results, memberships, archived):
    sources = {s['id']:s for s in catalog['sources']}; outcomes = {r['source_id']:r for r in results}
    pins = config['youtube_channels']; agencies = []
    for agency in config['agencies']:
        alternatives = []; keys = set()
        for identity in agency['alternatives']:
            if identity not in sources:continue
            aliases = {identity}
            source = sources[identity]
            if source['platform'] == 'telegram':
                def telegram_channel(url):
                    return urlsplit(url).path.strip('/').removeprefix('s/').split('/')[0].lower()
                owner = telegram_channel(source['url'])
                aliases.update(s['id'] for s in catalog['sources'] if s['platform']=='telegram'
                               and telegram_channel(s['url'])==owner)
            channel = pins.get(identity, {}).get('channel_id')
            if channel:
                aliases.update(s['id'] for s in catalog['sources'] if s['platform']=='youtube'
                               and urlsplit(s['url']).path.rstrip('/') == '/channel/'+channel)
                aliases.update(i for i,p in pins.items() if p['channel_id']==channel)
            found = set().union(*(memberships.get(i, set()) for i in aliases))
            keys.update(found)
            alternatives.append({'source_id':identity, 'url':sources[identity]['url'],
                'status':outcomes[identity]['status'], 'collected_posts':outcomes[identity]['posts'],
                'archive_matches_including_channel_aliases':len(found & archived)})
        agencies.append({'agency':agency['agency'], 'website_source_id':agency['website_source_id'],
                         'alternatives':alternatives, 'archived_posts':len(keys & archived),
                         'canonical_keys':sorted(keys & archived)})
    return agencies


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Archive verification requires GitHub-hosted Actions')
    p = argparse.ArgumentParser(); p.add_argument('--run-id', required=True); p.add_argument('--parts', required=True)
    p.add_argument('--registry', required=True); p.add_argument('--output', default='verification/report.json'); a = p.parse_args()
    if not a.run_id.isdigit(): raise ValueError('invalid_run_id')
    base = Path(__file__).resolve().parents[1]
    # New workflows preserve the exact selected catalog beside the registry.
    # Older all-platform artifacts did not include this snapshot.
    catalog_path=Path(a.registry).with_name('sources.json')
    if not catalog_path.is_file():catalog_path=base/'collect/sources.json'
    catalog, results, posts, memberships = read_parts(a.parts, a.run_id, catalog_path, a.registry)
    root = from_environment(); root.assert_private()
    report = root.read_json('collection-report-'+a.run_id+'.json')
    if not report or report['run_id'] != a.run_id or report['unique_posts'] != len(posts):
        raise ValueError('published_collection_report_mismatch')
    if {r['source_id']:r for r in report['sources']} != {r['source_id']:r for r in results}:
        raise ValueError('published_source_results_mismatch')
    if report.get('platform_scope')!=catalog.get('platform_scope'):raise ValueError('published_scope_mismatch')
    snapshot = load_snapshot(root); checks = verify_posts(posts, snapshot)
    real = [p for p in snapshot['items'] if not p.get('sample')]
    mapping = json.loads((base/'collect/platform-folders.json').read_text(encoding='utf-8'))
    if mapping['root'] != root.root: raise ValueError('unexpected_archive_root')
    counts = {}
    for platform, folder in mapping['platforms'].items():
        if catalog.get('platform_scope') and platform not in catalog['platform_scope']:continue
        store = DriveStore(root.service, folder['folder_id']); store.assert_private()
        view = store.read_json('posts.json'); local_report = store.read_json('collection-report.json')
        if not view or not local_report or local_report['run_id'] != a.run_id: raise ValueError('platform_view_not_updated')
        indexed = unique_items(view['posts'])
        expected = {p['id']:p for p in real if p['platform']==platform}
        if indexed != expected: raise ValueError('platform_view_mismatch')
        counts[platform] = len(indexed)
    full_catalog=json.loads((base/'collect/sources.json').read_text(encoding='utf-8'))
    saved_catalog=root.read_json('sources.json') or {'sources':[]}
    if not {s['id'] for s in full_catalog['sources']} <= {s['id'] for s in saved_catalog['sources']}:
        raise ValueError('full_source_catalog_not_preserved')
    cursors=root.read_json('collection-cursors.json') or {'sources':{}}
    for identity,value in report.get('state_updates',{}).items():
        if cursors['sources'].get(identity)!=value:raise ValueError('history_cursor_readback_mismatch')
    config = json.loads((base/'collect/dsns-official-sources.json').read_text(encoding='utf-8'))
    agencies = agency_report(config, catalog, results, memberships, {p['id'] for p in real})
    result = {'schema':'argus-archive-verification-v1', 'collection_run_id':a.run_id, 'verified_at':now(),
              'verified_sources':len(results), **checks, 'platform_views':counts, 'agencies':agencies,
              'preserved_catalog_sources':len(saved_catalog['sources']),
              'verified_history_cursors':len(report.get('state_updates',{})),
              'dsns_agencies_with_archived_posts':sum(bool(r['archived_posts']) for r in agencies),
              'dsns_unique_posts':len({key for r in agencies for key in r['canonical_keys']})}
    output = Path(a.output); output.parent.mkdir(parents=True, exist_ok=True)
    if catalog.get('platform_scope')==['facebook'] and any('full_text_with_date' in s for s in results):
        from facebook_coverage import render
        totals,body=render(a.run_id,results,posts)
        folder=DriveStore(root.service,mapping['platforms']['facebook']['folder_id'])
        name='facebook-feed-'+a.run_id+'.txt'
        file_id=folder.put(name,body)
        check=folder.find(name)
        if not check or check['md5Checksum']!=hashlib.md5(body,usedforsecurity=False).hexdigest():
            raise ValueError('facebook_report_readback_mismatch')
        result['facebook_coverage']={**totals,'drive_file_id':file_id}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('agencies','platform_views')}))


if __name__ == '__main__':
    try: main()
    except Exception as error:
        message = str(error) if isinstance(error, ValueError) and re.fullmatch('[a-z_]{1,70}', str(error)) else type(error).__name__
        print('Archive verification failed: '+message, file=sys.stderr)
        raise SystemExit(1)
