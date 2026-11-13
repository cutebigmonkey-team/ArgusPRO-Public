"""Report agency alternatives separately from unavailable website sources."""
from private_assets import private_asset_path
import datetime as dt
import json
from pathlib import Path


def coverage(results, posts, observed_at):
    settings = json.loads(private_asset_path('dsns-official-sources.json').read_text(encoding='utf-8'))
    by_id = {r['source_id']: r for r in results}
    by_source = {}
    for post in posts:
        by_source.setdefault(post.get('source_id'), []).append(post)
    observed = dt.datetime.fromisoformat(observed_at.replace('Z', '+00:00'))
    agencies = []
    for agency in settings['agencies']:
        alternatives = []
        for identity in agency['alternatives']:
            result = by_id.get(identity, {})
            rows = by_source.get(identity, [])
            stamps = [p['published_at'] for p in rows if p.get('published_at')]
            latest = result.get('latest_published_at') or max(stamps, default=None)
            age = (observed-dt.datetime.fromisoformat(latest.replace('Z', '+00:00'))).total_seconds()/86400 if latest else None
            alternatives.append({'source_id': identity, 'status': result.get('status', 'not_selected'),
                                 'posts': result.get('posts', len(rows)), 'latest_published_at': latest,
                                 'recent': age is not None and 0 <= age <= 30})
        agencies.append({'website_source_id': agency['website_source_id'], 'agency': agency['agency'],
                         'website_status': by_id.get(agency['website_source_id'], {}).get('status', 'not_selected'),
                         'alternatives': alternatives, 'covered': any(a['posts'] for a in alternatives),
                         'recent_covered': any(a['recent'] for a in alternatives)})
    return {'meaning': 'Existing official catalog channels; not full website article coverage',
            'agencies': agencies, 'covered': sum(a['covered'] for a in agencies),
            'recent_covered': sum(a['recent_covered'] for a in agencies), 'total': len(agencies)}
