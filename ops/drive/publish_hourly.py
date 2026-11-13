"""Publish only changed hourly posts; read back archive and check replay."""
from private_assets import private_asset_path
import json
import os
from pathlib import Path
import sys
import tempfile
from drive_store import DriveStore, from_environment
from publish import publish, load_snapshot
from record_quality import merge_observations
from url_identity import archive_identities, identity_keys
from platform_views import render_posts
from verify_collection import unique_items
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'local-storage'))
from storage import pack, timestamp


def changes(posts, snapshot):
    archived = unique_items(snapshot['items']); changed = []; new = 0
    aliases = archive_identities(snapshot['items'])
    observations = {}
    for post in posts:
        keys = identity_keys(post)
        key = next((aliases[k] for k in keys if k in aliases), keys[0])
        for k in keys: aliases.setdefault(k,key)
        row = {**post,'canonical_key':key}
        others = set(keys)-{key}
        if others: row['identity_aliases']=sorted(others)
        observations[key]=merge_observations(observations.get(key),row)
    fields = (('platform','platform'), ('post_url','url'), ('text','text'), ('published_at','published_at'),
              ('media_urls','media_urls'), ('text_may_be_truncated','text_may_be_truncated'),
              ('outbound_social_links','outbound_social_links'),('identity_aliases','identity_aliases'),
              ('outbound_profile_links','outbound_profile_links'),
              ('instagram_comments','instagram_comments'),('public_comments','public_comments'),
              ('video_analysis_text','video_analysis_text'),
              *((f,f) for f in ('title','category','language','keywords','engagement','likes','views','comments_count','region')))
    def value(row, field):
        result = row.get(field, [] if field in ('media_urls','outbound_social_links','outbound_profile_links') else None)
        return timestamp(result) if field=='published_at' and result else result
    for post in observations.values():
        old = archived.get(post['canonical_key'])
        if old is None: new += 1
        effective = merge_observations(old, {**post, 'url':post['post_url']})
        if old is None or any(value(effective,b) != value(old,b) for a,b in fields if a in post or b in effective): changed.append(post)
    return changed, new


def publish_delta(store, posts):
    before = load_snapshot(store); changed, new = changes(posts, before)
    if changed:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); media = root/'media'; media.mkdir()
            source = root/'posts.jsonl'; source.write_text(''.join(json.dumps(p, ensure_ascii=False)+'\n' for p in changed), encoding='utf-8')
            archive = root/'package.zip'; pack(source, media, archive)
            publish(store, archive)
            catalog = store.read_json('catalog.json')
            replay = publish(store, archive)
            if replay['status'] != 'already_imported' or store.read_json('catalog.json') != catalog:
                raise ValueError('hourly_replay_changed_archive')
    after = load_snapshot(store); indexed = unique_items(after['items'])
    if len(after['items']) != len(before['items']) + new: raise ValueError('unexpected_archive_growth')
    if changes(posts, after)[0]: raise ValueError('hourly_content_readback_mismatch')
    aliases=archive_identities(after['items'])
    if any(not any(k in aliases for k in identity_keys(p)) for p in posts): raise ValueError('hourly_post_missing')
    return after, {'new_posts':new, 'updated_posts':len(changed)-new, 'unchanged_posts':len(posts)-len(changed),
                   'archive_posts':len(indexed), 'duplicate_canonical_keys':0,
                   'replay':'unchanged' if changed else 'no_archive_write_needed'}, {p['platform'] for p in changed}


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Hourly publication requires GitHub-hosted Actions')
    directory = Path('collection/dsns-hourly')
    report = json.loads((directory/'report.json').read_text(encoding='utf-8'))
    with (directory/'posts.jsonl').open(encoding='utf-8') as stream:
        posts = [json.loads(line) for line in stream if line.strip()]
    root = from_environment(); root.assert_private()
    snapshot, result, changed_platforms = publish_delta(root, posts)
    mapping = json.loads(private_asset_path('platform-folders.json').read_text(encoding='utf-8'))
    if mapping['root'] != root.root: raise ValueError('unexpected_archive_root')
    verified_platforms = {}
    for platform in sorted({p['platform'] for p in posts}):
        folder = mapping['platforms'][platform]
        store = DriveStore(root.service, folder['folder_id']); store.assert_private()
        items = sorted((p for p in snapshot['items'] if p['platform']==platform and not p.get('sample')),
                       key=lambda p:(p.get('published_at') or '', p['id']), reverse=True)
        view = store.read_json('posts.json')
        # Also repairs a stale view after a previously interrupted publication.
        if view != {'posts':items}:
            store.json('posts.json', {'posts':items}, mutable=True)
            store.put('posts.txt', render_posts(items), mutable=True)
            view = store.read_json('posts.json')
        if unique_items(view['posts']) != {p['id']:p for p in items}: raise ValueError('hourly_view_mismatch')
        verified_platforms[platform] = len(items)
    report['publication'] = {**result, 'verified_platforms':verified_platforms,
                             'verified_collected_posts':len(posts)}
    root.json('dsns-hourly-latest.json', report, mutable=True)
    (directory/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report['publication']))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as out:
            out.write(f"DSNS: {len(posts)} posts verified in archive; {result['new_posts']} new, {result['updated_posts']} updated. Duplicate IDs: 0.\n")
    failed = [r for r in report['sources'] if r['status'] not in ('ok','no_public_posts','disabled_by_registry')]
    if failed: raise RuntimeError('Some DSNS sources failed; successful posts archived, inspect report')


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('Hourly publication failed: '+type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
