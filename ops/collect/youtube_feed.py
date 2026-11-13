"""Native YouTube Atom with channel ownership, never recommended-video IDs."""
from private_assets import private_asset_path
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, parse_qs
from bs4 import BeautifulSoup
import feedparser
from candidate_audit import count

CHANNEL = r'UC[A-Za-z0-9_-]{22}'


def page_channel(body):
    text = body.decode('utf-8', 'replace')
    soup = BeautifulSoup(text, 'html.parser')
    identities = set()
    for node in soup.select('meta[itemprop="channelId"][content]'):
        identities.add(node['content'])
    for node in soup.select('link[rel="alternate"][type="application/rss+xml"][href]'):
        url = urlsplit(node['href'])
        if url.hostname in ('youtube.com', 'www.youtube.com') and url.path == '/feeds/videos.xml':
            identities.update(parse_qs(url.query).get('channel_id', []))
    for match in re.finditer(r'"channelMetadataRenderer"\s*:\s*', text):
        try:
            value, _ = json.JSONDecoder().raw_decode(text[match.end():])
            identities.add(value.get('externalId', ''))
        except (ValueError, AttributeError):
            continue
    identities.discard('')
    if len(identities) != 1 or not re.fullmatch(CHANNEL, next(iter(identities), '')):
        raise ValueError('channel_owner_unresolved')
    return identities.pop()


def resolve(source, fetch, use_pins=True):
    if use_pins:
        settings = json.loads(private_asset_path('dsns-official-sources.json').read_text(encoding='utf-8'))
        pin = settings.get('youtube_channels', {}).get(source.get('id'))
        if pin and pin['source_url'] == source['url'] and re.fullmatch(CHANNEL, pin['channel_id']):
            return pin['channel_id']
    match = re.fullmatch('/channel/(' + CHANNEL + ')/?', urlsplit(source['url']).path)
    if match:
        return match[1]
    body, _ = fetch(source['url'])
    return page_channel(body)


def parse(source, body, identity, limit):
    from run import feed_entries
    parsed = feedparser.parse(body)
    # YouTube currently omits UC in yt:channelId on some feeds.
    if parsed.feed.get('yt_channelid') not in (identity, identity[2:]):
        raise ValueError('feed_owner_mismatch')
    owner = urlsplit(parsed.feed.get('link', ''))
    if owner.hostname not in ('www.youtube.com', 'youtube.com') or owner.path.rstrip('/') != '/channel/' + identity:
        raise ValueError('feed_owner_mismatch')
    accepted=[]
    for entry in parsed.entries:
        url = urlsplit(entry.get('link', ''))
        video = entry.get('yt_videoid', '')
        video_link = ((url.path == '/watch' and parse_qs(url.query).get('v') == [video])
                      or url.path == '/shorts/' + video)
        if (entry.get('yt_channelid') not in (identity, identity[2:])
                or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video)
                or url.scheme != 'https' or url.username or url.password
                or url.hostname not in ('www.youtube.com', 'youtube.com')
                or not video_link):
            count('raw_input')
            count('feed_entry_owner_or_video_mismatch')
            continue
        accepted.append(entry)
    if parsed.entries and not accepted:
        raise ValueError('feed_entry_owner_mismatch')
    items = feed_entries(source, accepted, limit)
    entries = {entry.get('yt_videoid'): entry for entry in accepted}
    for item in items:
        item.update(capture_mode='youtube_native_atom', text_may_be_truncated=True)
        entry = entries.get(item['canonical_key'].split(':',1)[1], {})
        metrics = {}
        # Atom media:statistics is a publisher view count. The star-rating
        # sample count is not a like count and must not be relabelled as one.
        raw = entry.get('media_statistics', {}).get('views')
        if isinstance(raw, str) and raw.isdigit(): metrics['views'] = int(raw)
        if metrics: item['engagement'] = metrics
    return items


def collect(source, limit, fetch):
    identity = resolve(source, fetch)
    body, _ = fetch('https://www.youtube.com/feeds/videos.xml?channel_id=' + identity)
    return parse(source, body, identity, limit)
