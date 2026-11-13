"""Verify which public publication text falls within each operator opt-in."""
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

RESTRICTED = re.compile(r'для\s+службового\s+користування|конфіденційно|цілком\s+таємно|не\s+для\s+(?:поширення|оприлюднення)|(?:api[_ -]?key|password|bearer)\s*[:=]', re.I)


def public_url(value):
    try:
        u = urlsplit(value or '')
        host = u.hostname or ''
        if (u.scheme == 'https' and u.username is None and u.password is None and u.port in (None, 443)
                and re.fullmatch(r'(?:[a-z0-9-]+\.)+[a-z]{2,63}', host) and not u.fragment
                and not host.endswith(('.internal', '.local', '.test', '.invalid', '.localhost'))
                and not re.search(r'(?:token|signature|credential|auth|session|secret|password|api[_-]?key)', u.query, re.I)):
            return u
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def verified_public_archive_text(item):
    """Match a full, anonymous native capture to its source and permalink.

    A public headline, a source label or an accessible profile is insufficient.
    Only authorize the captured publication body, never enrichment or comments.
    """
    body = item.get('text')
    if (item.get('sample') or item.get('internal') or item.get('confidential')
            or item.get('visibility', 'public') != 'public' or item.get('source_match') == 'search_query_only'
            or item.get('retained_text_observed_at') or item.get('facebook_retained_text') or item.get('video_analysis_text')
            or not isinstance(body, str) or not body.strip() or RESTRICTED.search(body)):
        return False
    url, source = public_url(item.get('url')), public_url(item.get('source_url'))
    if not url or not source or not item.get('source_id'): return False
    platform, mode, tool = item.get('platform'), item.get('capture_mode'), item.get('tool')
    if mode and mode != tool: return False
    host = url.hostname.removeprefix('www.')
    source_host = source.hostname.removeprefix('www.')
    if platform == 'telegram' and tool in ('telegram_public_history', 'tg_radar_public_html'):
        post = re.fullmatch(r'/([A-Za-z0-9_]+)/([1-9][0-9]*)/?', url.path)
        channel = re.fullmatch(r'/(?:s/)?([A-Za-z0-9_]+)/?', source.path)
        # These readers parse an individual data-post from the anonymous /s/
        # channel page. Legacy native rows do not store a truncation flag.
        return bool(host in ('t.me','telegram.me') and source_host in ('t.me','telegram.me')
                    and not url.query and not source.query and post and channel
                    and post[1].lower() == channel[1].lower()
                    and item.get('id') == 'telegram:'+post[1].lower()+'/'+post[2]
                    and (item.get('text_may_be_truncated') is None or item.get('text_may_be_truncated') is False))
    if item.get('text_may_be_truncated') is not False: return False
    if platform in ('rss', 'mailru') and tool in ('native_feed', 'structured_article', 'trafilatura_article'):
        return host == source_host and bool(url.path.strip('/')) and not url.query
    if platform == 'facebook' and tool in ('facebook_public_story', 'visible_public_card'):
        return bool(host in ('facebook.com','m.facebook.com') and source_host in ('facebook.com','m.facebook.com')
                    and item.get('facebook_text_source') in ('story_message','expanded_card')
                    and (re.fullmatch(r'/(?:[^/]+/)?(?:posts|videos|reel)/[A-Za-z0-9]+/?', url.path)
                         or (url.path in ('/story.php','/permalink.php') and re.fullmatch(r'(?:story_fbid=[A-Za-z0-9]+&id=[0-9]+|id=[0-9]+&story_fbid=[A-Za-z0-9]+)',url.query))))
    if platform == 'youtube' and tool == 'youtube_public_watch':
        return host == source_host == 'youtube.com' and url.path == '/watch' and bool(re.fullmatch(r'v=[\w-]{11}',url.query))
    return False

def official_channels():
    root = Path(__file__).resolve().parents[1] / 'collect'
    policy = json.loads((root / 'dsns-official-sources.json').read_text(encoding='utf-8'))
    sources = json.loads((root / 'sources.json').read_text(encoding='utf-8'))['sources']
    ids = {key for agency in policy['agencies'] for key in agency['alternatives']}
    return frozenset(urlsplit(s['url']).path.strip('/').removeprefix('s/').split('/')[0].lower()
        for s in sources if s['id'] in ids and s['platform'] == 'telegram' and urlsplit(s['url']).hostname == 't.me')

def verified_public(item, channels):
    if item.get('sample') or item.get('capture_mode') == 'search_preview' or item.get('source_match') == 'search_query_only': return False
    if item.get('text_may_be_truncated'): return False
    if item.get('visibility', 'public') != 'public' or item.get('confidential') or item.get('internal'): return False
    text = item.get('text')
    if not isinstance(text, str) or not text.strip() or RESTRICTED.search(text): return False
    try:
        url = urlsplit(item.get('url', ''))
        if url.scheme != 'https' or url.username or url.password or url.port not in (None,443): return False
        host = url.hostname or ''
        if item.get('platform') in ('rss','web','website') and (host == 'dsns.gov.ua' or host.endswith('.dsns.gov.ua')):
            return True
        if item.get('platform') == 'telegram' and host in ('t.me','telegram.me'):
            parts = url.path.strip('/').removeprefix('s/').split('/')
            return len(parts)==2 and parts[0].lower() in channels and parts[1].isdigit() and not url.query and not url.fragment
    except (ValueError, TypeError): return False
    # Historical YouTube/Facebook snapshots lack channel ownership evidence;
    # a source name alone is never a privacy authorization.
    return False
