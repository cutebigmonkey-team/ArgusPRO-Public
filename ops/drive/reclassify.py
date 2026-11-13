"""Backfill only origin, using the original packages as publisher evidence."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from drive_store import DriveStore

from publish import catalog_origins, commit_snapshot, encoded, from_environment, load_snapshot, now
from url_identity import archive_identities, identity_keys


def reclassify(store, *, apply=False):
    store.assert_private()
    pointer = store.read_json('catalog.json')
    snapshot = load_snapshot(store)
    origins = catalog_origins(store)
    aliases = archive_identities(snapshot['items'])
    evidence = defaultdict(set)
    for package_id in snapshot['packages']:
        package = store.read_json('package-' + package_id + '.json')
        if not package or package['manifest']['package_id'] != package_id:
            raise ValueError('Missing original publication package')
        for post in package['posts']:
            origin = origins.resolve(post)
            if origin != 'unknown':
                matches = {aliases[k] for k in identity_keys(post) if k in aliases}
                if len(matches) == 1:
                    evidence[matches.pop()].add(origin)

    counts = Counter()
    changed = Counter()
    rows = []
    for row in snapshot['items']:
        candidates = evidence[row['id']].copy()
        direct = origins.resolve(row)
        if direct != 'unknown':
            candidates.add(direct)
        origin = candidates.pop() if len(candidates) == 1 else 'unknown'
        if row.get('source_match') == 'search_query_only' or row.get('capture_mode') == 'search_preview':
            origin = 'unknown'
        rows.append({**row, 'origin': origin})
        counts[origin] += 1
        if row.get('origin') != origin:
            changed[origin] += 1
    if apply and changed:
        updated = {**snapshot, 'items': rows, 'updated_at': now()}
        if store.read_json('catalog.json') != pointer:
            raise RuntimeError('Archive changed during reclassification')
        commit_snapshot(store, updated, encoded(updated))
        if load_snapshot(store) != updated:
            raise RuntimeError('Reclassification readback mismatch')
    return {'records':len(rows), 'packages':len(snapshot['packages']), 'origins':dict(counts),
            'changes':dict(changed), 'changed_records':sum(changed.values()), 'applied':apply}


def update_platform_views(root):
    mapping = json.loads((Path(__file__).resolve().parents[1]/'collect/platform-folders.json').read_bytes())
    if root.root != mapping['root']:
        raise ValueError('Unexpected archive folder')
    origins = {(r['platform'], r['id']):r['origin'] for r in load_snapshot(root)['items']}
    for platform, descriptor in mapping['platforms'].items():
        folder = DriveStore(root.service, descriptor['folder_id'])
        metadata = root.service.files().get(fileId=folder.root, fields='parents,name').execute(num_retries=3)
        if root.root not in metadata.get('parents', []) or metadata['name'] != descriptor['name']:
            raise ValueError('Unexpected platform folder')
        folder.assert_private()
        view = folder.read_json('posts.json')
        if view is None:
            continue
        changed = False
        for row in view['posts']:
            origin = origins[(platform, row['id'])]
            if row.get('origin') != origin:
                row['origin'] = origin
                changed = True
        if changed:
            folder.json('posts.json', view, mutable=True)
            if folder.read_json('posts.json') != view:
                raise RuntimeError('Platform view readback mismatch')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    store = from_environment()
    apply = parser.parse_args().apply
    print(json.dumps(reclassify(store, apply=apply)), flush=True)
    if apply:
        update_platform_views(store)
