"""Passive dependency/route inventory. Never starts services or opens sockets."""
import os
from pathlib import Path
from urllib.parse import urlsplit
import re


def unavailable_reason(tool,environ=None):
    environ=os.environ if environ is None else environ
    if tool=='pravaha' and environ.get('ARGUS_COLLECTOR_RUNTIME')=='hostless':
        # The supported worker image has no local timeline service. Do not
        # spend source budgets retrying its fixed loopback endpoint.
        return 'local_service_not_in_image'
    executable={'threads-cli':'tamnd-th','facebook-cli':'fb','threads-agent':'th',
                'python-social':'python/bin/python','remaining-python':'remaining-python/bin/python',
                'short-post-python':'short-post-python/bin/python','telegramchannels':'telegramchannels'}.get(tool)
    if executable:
        binary=Path(environ.get('ARGUS_SOCIAL_TOOLS','.social-tools'))/executable
        if not binary.is_file() or not os.access(binary,os.X_OK):return 'executable_missing'
    return None


def instagram_gateway_candidate(source):
    return (source.get('platform')=='instagram' and
            urlsplit(source.get('url','')).hostname in {'instagram.com','www.instagram.com'} and
            bool(re.fullmatch(r'/[A-Za-z0-9_.]{1,30}/?',urlsplit(source['url']).path)) and
            urlsplit(source['url']).path.strip('/').lower() not in {'accounts','explore','direct'})


def incremental_route(source):
    platform=source.get('platform')
    return {'telegram':'telegram_public_history','rss':'public_feed_history',
            'mailru':'public_feed_history','youtube':'youtube_public_history'}.get(platform,'collect_source')


def route_inventory(catalog,environ=None):
    """Describe actual incremental switches without invoking any adapter."""
    environ=os.environ if environ is None else environ
    import importlib.util
    rows=[]
    fallbacks={'instagram':['rss_bridge','optional_public_adapters','browser_public'],
        'x':['optional_primary','public_embed','browser_public'],
        'threads':['optional_primary','threads_public_json_or_social_adapter','browser_public'],
        'facebook':['public_feed_browser','optional_public_adapters'],
        'vk':['optional_public_adapters','browser_public'],
        'ok':['optional_public_adapters','browser_public'],
        'tiktok':['public_embed','optional_public_adapters','browser_public'],
        'reddit':['public_feeds'],'gdelt':['gdelt_public_json'],
        'rutube':['public_video_metadata'],'whatsapp':['unsupported_anonymous'],
        'max':['unsupported_anonymous']}
    switches=('ARGUS_SOURCE_ADAPTERS','ARGUS_SOCIAL_COLLECTION','ARGUS_EMBED_COLLECTION',
              'ARGUS_REMAINING_COLLECTION','ARGUS_CANDIDATE_FEEDS','ARGUS_ENHANCED_COLLECTION',
              'ARGUS_FACEBOOK_X','ARGUS_PUBLIC_SOCIAL_ADAPTERS')
    for platform in sorted({s['platform'] for s in catalog.get('sources',[])}):
        primary={'x':'pravaha','threads':'threads-cli'}.get(platform) if environ.get('ARGUS_SOURCE_ADAPTERS')=='1' else None
        rows.append({'platform':platform,'incremental_route':incremental_route({'platform':platform}),
                     'optional_primary':primary,'primary_unavailable_reason':unavailable_reason(primary,environ) if primary else None,
                     'fallbacks':fallbacks.get(platform,[]),
                     'fallback_execution_verified':False})
    return {'platforms':rows,'optional_dependency_checks':{tool:unavailable_reason(tool,environ) or 'NOT_VERIFIED'
                for tool in ('pravaha','threads-cli','facebook-cli','threads-agent','python-social','remaining-python','short-post-python','telegramchannels')},
            'enabled_switches':[name for name in switches if environ.get(name)=='1'],
            'python_modules_present':{name:importlib.util.find_spec(name) is not None for name in ('playwright','feedparser','crawlee','crawl4ai','scrapy')},
            'scope':'passive current-environment check; module presence is not extraction success'}
