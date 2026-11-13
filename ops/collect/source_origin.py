"""Resolve the existing catalog origin using publisher identifiers only."""
from private_assets import private_asset_path
import json
import re
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

from import_sheet import normalize, is_catalog_addition


def catalog_url(url):
    try:
        platform, canonical = normalize(url)
        if platform == 'youtube' and urlsplit(canonical).path == '/feeds/videos.xml':
            channels = parse_qs(urlsplit(url).query).get('channel_id', [])
            if len(channels) != 1 or not re.fullmatch(r'UC[A-Za-z0-9_-]{22}', channels[0]):
                return None
            canonical = 'https://www.youtube.com/channel/' + channels[0]
        return platform, canonical
    except (AttributeError, ValueError, UnicodeError):
        return None


class SourceOrigins:
    def __init__(self, sources):
        self.by_id = {}
        self.metadata = {}
        self.by_url = {}
        pins = json.loads(private_asset_path('dsns-official-sources.json').read_bytes())['youtube_channels']
        for source in sources:
            origin = source.get('origin', 'unknown')
            origin = origin if origin in ('general', 'dsns') else 'unknown'
            platform = source['platform']
            self.metadata[(platform, source['id'])] = source
            self.by_id[(platform, source['id'])] = origin
            url = catalog_url(source.get('url'))
            if url and url[0] == platform:
                self.by_url.setdefault(url, set()).add(origin)
                pin = pins.get(source['id'])
                if platform == 'youtube' and pin and pin['source_url'] == source['url']:
                    native = (platform, 'https://www.youtube.com/channel/' + pin['channel_id'])
                    self.by_url.setdefault(native, set()).add(origin)

    def resolve(self, item):
        if item.get('source_match') == 'search_query_only' or item.get('capture_mode') == 'search_preview':
            return 'unknown'
        platform = item['platform']
        by_id = self.by_id.get((platform, item.get('source_id')))
        url = catalog_url(item.get('source_url'))
        candidates = set(self.by_url.get(url, ())) if url and url[0] == platform else set()
        if by_id:
            candidates.add(by_id)
        return candidates.pop() if len(candidates) == 1 else 'unknown'


def load_shipped_sources():
    return json.loads(Path(__file__).with_name('sources.json').read_bytes())


def catalog_origins(store, *, register=False):
    catalog = store.read_json('sources.json')
    shipped = load_shipped_sources()
    if catalog is None:
        return SourceOrigins(shipped['sources'])
    # The normal writer commits the first package before refreshing sources.json in Drive.
    # Existing Drive decisions still take precedence for previously registered IDs.
    registered = {s['id'] for s in catalog['sources']}
    additions = [s for s in shipped['sources'] if s['id'] not in registered and is_catalog_addition(s)]
    if register and additions:
        registered_urls={catalog_url(s.get('url')) for s in catalog['sources']}
        if any(catalog_url(s['url']) in registered_urls for s in additions):
            raise ValueError('Reviewed profile already registered under a different ID')
        updated={**catalog,'sources':catalog['sources']+additions}
        store.json('sources.json',updated,mutable=True)
        if store.read_json('sources.json')!=updated:
            raise ValueError('Source catalog registration readback failed')
    return SourceOrigins(catalog['sources'] + additions)
