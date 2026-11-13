"""Discover publisher endpoints. Only explicitly advertised, readable, same-site feeds auto-enrol."""
from collections import Counter
from hashlib import sha256
import json
from itertools import zip_longest
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
import re
import time
from urllib.parse import parse_qsl, urljoin, urlsplit

from import_sheet import normalize
from source_origin import catalog_url
from url_identity import same_site, url_ok

PROFILES = {
    'facebook': r'/(?:[A-Za-z0-9_.-]+|profile\.php\?id=\d+)',
    'instagram': r'/[A-Za-z0-9_.]{1,30}', 'x': r'/[A-Za-z0-9_]{1,15}',
    'threads': r'/@[A-Za-z0-9_.]+', 'tiktok': r'/@[A-Za-z0-9_.]+',
    'telegram': r'/[A-Za-z][A-Za-z0-9_]{3,63}',
    'youtube': r'/(?:@[\w.%+-]+|channel/UC[A-Za-z0-9_-]{22}|(?:c|user)/[\w.%+-]+)',
    'rutube': r'/channel/\d+', 'vk': r'/[\w.]+', 'ok': r'/(?:group|profile)/\d+',
}
RESERVED = {'share','sharer','sharer.php','p','reel','reels','intent','i','search','home','login',
            'explore','accounts','dialog','stories','groups','watch','plugins','settings','help',
            'joinchat','share.php','login.php','iv','proxy','socks','addstickers','contact','privacy'}
DIRECTORIES = {
    'https://www.kmu.gov.ua/catalog': 'government_directory',
    'https://imi.org.ua/monitorings/bilyj-spysok-imi-za-pershe-pivrichchya-2026-roku-uvijshly-18-media': 'media_directory',
    'https://www.europarl.europa.eu/at-your-service/en/stay-informed/rss-feeds': 'rss_directory',
}


def profile(url):
    if not isinstance(url,str) or len(url)>2048 or not url_ok(url):
        return None
    # A login redirect or an invite with a start token is not a public profile.
    if any(k.lower() in ('next','start','startgroup','invite','token') for k,_ in parse_qsl(urlsplit(url).query)):
        return None
    try:
        platform, canonical = normalize(url)
        p = urlsplit(canonical)
        path = p.path + ('?' + p.query if p.query else '')
        if (p.hostname in ('youtu.be','vt.tiktok.com','vm.tiktok.com') or p.path.strip('/').lower() in RESERVED or
                not re.fullmatch(PROFILES.get(platform, r'(?!)'), path)):
            return None
        return platform, canonical
    except (ValueError, UnicodeError, AttributeError):
        return None


def seeds(catalog):
    publishers, official, other = set(), set(), set()
    for source in catalog:
        if not source.get('enabled', True):
            continue
        if source['platform'] in ('rss', 'mailru'):
            host=urlsplit(source['url']).hostname
            (official if host and host.endswith('.gov.ua') else other).add(source['url'])
        for ref in source.get('references', []):
            url = ref.get('url', '')
            if ref.get('kind') == 'verified_public_profile' and ref.get('owner_kind') == 'media' and url_ok(url):
                if normalize(url)[0] == 'rss':
                    publishers.add(url)
    sort_key=lambda u:(not (urlsplit(u).hostname or '').endswith('.ua'),u)
    groups=[sorted(group,key=sort_key) for group in (publishers,official,other)]
    return list(dict.fromkeys(url for turn in zip_longest(*groups) for url in turn if url))


def category(url):
    host = urlsplit(url).hostname or ''
    if host == 'dsns.gov.ua' or host.endswith('.dsns.gov.ua'):
        return 'dsns'
    if host.endswith('.gov.ua') or host.removeprefix('www.')=='europarl.europa.eu':
        return 'government'
    return 'public_information'


def registered_category(source):
    if source.get('category'):
        return source['category']
    for ref in source.get('references',[]):
        if ref.get('kind')=='verified_public_profile':
            label={'media':'media','politician':'public_official'}.get(ref.get('owner_kind'))
            if label:
                return label
    return category(source.get('url',''))


def publisher_category(soup, page):
    """Classify only the site's own structured publisher, never people mentioned in articles."""
    kind = category(page)
    if kind != 'public_information':
        return kind, 'official_domain'
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (ValueError, RecursionError):
            continue
        nodes = data.get('@graph',[data]) if isinstance(data,dict) else data
        if not isinstance(nodes,list):
            continue
        for node in nodes:
            if not isinstance(node,dict) or not same_site(node.get('url',''),page):
                continue
            if urlsplit(node['url']).path.rstrip('/') != urlsplit(page).path.rstrip('/'):
                continue
            types = node.get('@type',[])
            types = [types] if isinstance(types,str) else types
            if not isinstance(types,list):
                continue
            if 'NewsMediaOrganization' in types:
                area = node.get('areaServed')
                scope = area.get('@type') if isinstance(area,dict) else None
                return {'Country':'national_media','AdministrativeArea':'regional_media',
                        'City':'local_media'}.get(scope,'media'), 'publisher_schema'
            for schema, label in [('PoliceStation','law_enforcement'),('NGO','civil_society'),
                                  ('ResearchOrganization','analysis'),('GovernmentOrganization','government')]:
                if schema in types:
                    return label, 'publisher_schema'
            if 'Person' in types:
                job = str(node.get('jobTitle','')).lower().strip()
                roles = {'journalist':'journalist','журналіст':'journalist','журналістка':'journalist',
                         'blogger':'blogger','блогер':'blogger','блогерка':'blogger',
                         'member of parliament':'public_official','народний депутат україни':'public_official',
                         'minister':'public_official'}
                if job in roles:
                    return roles[job], 'publisher_schema'
    return kind, 'unclassified'


def candidate(platform, url, method, evidence, stamp):
    return {'id': 'discovery-' + sha256((platform+'|'+url).encode()).hexdigest()[:24],
            'platform': platform, 'url': url, 'discovered_at': stamp, 'checked_at': stamp,
            'method': method, 'evidence_url': evidence, 'status': 'review',
            'category': 'public_information', 'confidence': 'unconfirmed'}


def public_profiles(urls, method, evidence, stamp):
    for link in urls:
        target = profile(link)
        if target:
            row = candidate(*target, method, evidence, stamp)
            if method == 'search_result':
                row['query'] = row.pop('evidence_url')
                row['evidence_url'] = target[1]
            yield row


def mentioned_profiles(post):
    origin = post.get('url') or post.get('post_url')
    if post.get('sample') or not url_ok(origin):
        return []
    links = re.findall(r'https?://[^\s<>"\u200b]+', post.get('text',''))+post.get('outbound_profile_links',[])
    return public_profiles((u.rstrip('.,;:!?)]}') for u in links),
                           'archived_publication', origin, post['observed_at'])


def source_counts(rows):
    totals = {}
    for row in rows:
        counts = totals.setdefault(row['platform'], Counter())
        counts[row['status']] += 1
    return totals


def registered_discoveries(catalog):
    return [s for s in (catalog or {}).get('sources',[]) if
            (s['id'].startswith('discovery-') and any(r.get('kind') in
             ('discovered_public_feed','discovered_public_channel','reviewed_public_source') for r in s.get('references',[])))
            or (s['id'].startswith('web-') and {'kind':'site_submission'} in s.get('references',[]))]


def add_registered(catalog, saved):
    known = {catalog_url(s.get('url')) for s in catalog['sources']}
    ids = {s['id'] for s in catalog['sources']}
    sources = list(catalog['sources'])
    for row in registered_discoveries(saved):
        key = catalog_url(row['url'])
        if key and row['id'] not in ids and key not in known:
            sources.append(row)
            ids.add(row['id'])
            known.add(key)
    return {**catalog, 'sources':sources}


def publisher_links(body, page, stamp):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, 'html.parser')
    kind, basis = publisher_category(soup, page)
    for tag in soup.select('link[href], a[href], outline[xmlurl]'):
        try:
            url = urljoin(page, tag.get('href') or tag.get('xmlurl'))
            if not url_ok(url) or len(url) > 2048 or any(
                    re.search(r'token|secret|password|auth|session|email|api.?key',k,re.I)
                    for k,_ in parse_qsl(urlsplit(url).query)):
                continue
            platform, canonical = normalize(url)
        except (ValueError, UnicodeError):
            continue
        if platform in ('rss', 'mailru'):
            alternate = (tag.name == 'link' and tag.find_parent('head') and 'alternate' in tag.get('rel', []) and
                         tag.get('type', '').lower() in ('application/rss+xml','application/atom+xml'))
            listed_feed=(DIRECTORIES.get(page.rstrip('/'))=='rss_directory' and tag.name=='a' and
                         bool(re.search(r'\b(?:RSS|XML)\b',tag.get_text(' ',strip=True),re.I)))
            if alternate or tag.name == 'outline' or listed_feed:
                publisher_feed=(alternate or listed_feed) and same_site(url,page)
                row = candidate(platform, canonical, 'publisher_feed' if publisher_feed else 'feed_directory', page, stamp)
                # www aliases share an identity but can have different DNS or redirect behavior.
                row['url']=urlsplit(canonical)._replace(netloc=urlsplit(url).hostname).geturl()
                if publisher_feed:
                    row.update(category=kind, category_basis=basis)
                yield row
            elif page.rstrip('/') in DIRECTORIES and not same_site(url,page) and not urlsplit(url).query:
                method = DIRECTORIES[page.rstrip('/')]
                if method=='rss_directory':
                    continue
                if method == 'government_directory' and not urlsplit(url).hostname.endswith('.gov.ua'):
                    continue
                if method == 'media_directory' and (not tag.find_parent('article') or urlsplit(url).path.strip('/')):
                    continue
                website = 'https://'+urlsplit(canonical).hostname+'/'
                yield candidate(platform, website, method, page, stamp)
        else:
            for row in public_profiles([url], 'publisher_link', page, stamp):
                if row['platform']=='telegram' and ('me' in tag.get('rel',[]) or tag.find_parent('footer')):
                    row.update(method='publisher_channel_link',publisher_category=kind,category_basis=basis)
                yield row


def inspect_feed(row, crawl):
    import feedparser
    from run import feed_entries
    body, final = crawl.read(row['url'])
    parsed = feedparser.parse(body)
    if not parsed.version or not same_site(final, row['evidence_url']):
        row['reason'] = 'unverified_feed'
        return
    source = {**row, 'name': urlsplit(final).hostname}
    posts = feed_entries(source, parsed.entries, len(parsed.entries), base_url=final)
    owned = [p for p in posts if same_site(p['post_url'], final)]
    row.update(raw_entries=len(parsed.entries), readable_publications=len(owned))
    if owned:
        row.update(status='confirmed', confidence='high',
                   sample_permalink=owned[0]['post_url'])
    else:
        row['reason'] = 'no_owned_publications'


def inspect_channel(row, reader):
    from bs4 import BeautifulSoup
    from run import telegram_html
    handle=urlsplit(row['url']).path.strip('/')
    preview='https://t.me/s/'+handle
    body,final=reader({'platform':'rss','url':preview},100,1).read(preview)
    if final.rstrip('/').lower()!=preview.lower():
        row['reason']='unexpected_channel_redirect'
        return
    soup=BeautifulSoup(body,'html.parser')
    backlinks=[a.get('href','') for a in soup.select('.tgme_channel_info_description a[href]')]
    if not any(same_site(link,row['evidence_url']) and
               urlsplit(link).path.rstrip('/')==urlsplit(row['evidence_url']).path.rstrip('/')
               for link in backlinks):
        row['reason']='publisher_backlink_missing'
        return
    posts=telegram_html(row,100,body)
    if posts:
        row.update(status='confirmed',confidence='high',category=row['publisher_category'],
                   readable_publications=len(posts),sample_permalink=posts[-1]['post_url'])
    else:
        row['reason']='no_owned_publications'


def note_cooldown(cooldowns, host, error, stamp):
    if not isinstance(error,HTTPError) or error.code not in (429,503):
        return
    when=datetime.fromisoformat(stamp)
    until=when+timedelta(hours=1)
    value=(error.headers or {}).get('Retry-After','')
    if value:
        try:
            hint=when+timedelta(seconds=int(value)) if value.isdigit() else parsedate_to_datetime(value)
            until=max(until,hint)
        except (ValueError,TypeError,OverflowError):
            pass
    cooldowns[host]=until.astimezone(timezone.utc).isoformat()


def scan(catalog, cursor='', disabled=(), *, directory_sites=(), cooldowns=None, validation_cursors=None,
         budget=4, seconds=65, reader=None):
    from crawl_common import CrawlState
    from run import error_code, now
    reader=reader or CrawlState
    cooldowns = {} if cooldowns is None else cooldowns
    validation_cursors = {} if validation_cursors is None else validation_cursors
    sites = list(dict.fromkeys([*DIRECTORIES,*seeds(catalog),*sorted(directory_sites)]))
    start = sites.index(cursor)+1 if cursor in sites else 0
    candidates, attempts = [], []
    channel_checked=False
    known = {catalog_url(s['url']) for s in catalog}
    deadline = time.monotonic() + seconds
    for site in (sites[start:] + sites[:start])[:budget]:
        if time.monotonic() >= deadline:
            break
        cursor = site
        stamp = now()
        host = urlsplit(site).hostname.removeprefix('www.')
        if cooldowns.get(host,'') > stamp:
            attempts.append({'url':site,'status':'deferred_rate_limit'})
            continue
        cooldowns.pop(host,None)
        crawl = reader({'platform':'rss', 'url':site}, 1000, 5)
        try:
            body, final = crawl.read(site)
            links = {}
            for row in publisher_links(body,final,stamp):
                if row['platform'] in disabled or catalog_url(row['url']) in known:
                    continue
                if links.get(row['url'],{}).get('method')!='publisher_channel_link':
                    links[row['url']]=row
            addresses=list(links)
            last=validation_cursors.get(site)
            offset=addresses.index(last)+1 if last in links else 0
            for address in addresses[offset:]+addresses[:offset]:
                row=links[address]
                if (row['method'] == 'publisher_feed' and same_site(row['url'], final) and
                        not crawl.stopped and host not in cooldowns and crawl.pages < crawl.max_pages and time.monotonic() < deadline):
                    validation_cursors[site]=address
                    try:
                        inspect_feed(row, crawl)
                    except Exception as exc:
                        row['reason'] = error_code(exc)
                        note_cooldown(cooldowns,host,exc,stamp)
                elif (row['method']=='publisher_channel_link' and not channel_checked and
                      cooldowns.get('t.me','')<=stamp and time.monotonic()<deadline):
                    channel_checked=True
                    validation_cursors[site]=address
                    try:
                        inspect_channel(row,reader)
                    except Exception as exc:
                        row['reason']=error_code(exc)
                        note_cooldown(cooldowns,'t.me',exc,stamp)
                if row['status']=='review' and not row.get('reason'):
                    row['reason']='awaiting_validation' if row['method'] in ('publisher_feed','publisher_channel_link') else 'identity_review_required'
                candidates.append(row)
            attempts.append({'url':site,'status':'ok','candidates':len(links)})
        except Exception as exc:
            attempts.append({'url':site,'status':error_code(exc)})
            note_cooldown(cooldowns,host,exc,stamp)
    return candidates, cursor, attempts


def merge_candidates(previous, incoming):
    by_url = {row['id']:row for row in previous}
    for row in incoming:
        old = by_url.get(row['id'])
        evidence={'method':row['method'],'url':row['evidence_url']}
        if row.get('query'):
            evidence['query']=row['query']
        history=list((old or {}).get('evidence',[]))
        if evidence not in history and len(history)<12:
            history.append(evidence)
        row={**row,'evidence':history}
        if old:
            # A weak mention cannot reverse confirmation or replace a moderator decision.
            if old['status'] in ('rejected','approved') or (
                    old['status'] == 'confirmed' and row['status'] != 'confirmed'):
                by_url[row['id']]={**old,'evidence':history}
                continue
            if old['checked_at'] > row['checked_at'] and old['status'] == row['status']:
                continue
            row = {**row, 'discovered_at':min(old['discovered_at'], row['discovered_at'])}
        by_url[row['id']] = row
    return sorted(by_url.values(), key=lambda row:row['url'])


def apply_reviews(rows, decisions):
    """Decisions come from the deployed, reviewed repository revision only."""
    categories={'government','dsns','public_official','law_enforcement','media','national_media',
                'regional_media','local_media','journalist','blogger','civil_society','analysis','public_information'}
    result=[]
    for row in rows:
        decision=decisions.get(row['url'])
        if decision is None:
            result.append(row)
            continue
        if decision.get('status')=='rejected':
            result.append({**row,'status':'rejected'})
            continue
        evidence=decision.get('evidence_url','')
        post=decision.get('sample_permalink','')
        if (decision.get('status')!='approved' or decision.get('category') not in categories or
                not url_ok(evidence) or not same_site(post,row['url']) or
                catalog_url(post)==catalog_url(row['url']) or not decision.get('name') or
                (decision['category']=='dsns' and category(evidence)!='dsns')):
            raise ValueError('invalid_source_review')
        result.append({**row,'status':'approved','confidence':'reviewed','category':decision['category'],
                       'name':decision['name'],'review_evidence':evidence,'sample_permalink':post})
    return result


def moderation_queue(previous, candidates, limit=5000):
    """Retain existing tickets and all decisions. Count excess new leads for a later discovery pass."""
    existing={r['id'] for r in previous}
    result=[r for r in candidates if r['status']!='review' or r['id'] in existing]
    capacity=max(0,limit-sum(r['status']=='review' for r in result))
    new=[r for r in candidates if r['status']=='review' and r['id'] not in existing]
    return sorted(result+new[:capacity],key=lambda r:r['url']),dict(Counter(r['platform'] for r in new[capacity:]))


def enroll(catalog, candidates):
    """Derive scheduler rows from verified evidence. Existing IDs and switches win."""
    rejected={r['id'] for r in candidates if r['status']=='rejected'}
    sources = [{**s,'enabled':False} if s['id'] in rejected and s['id'].startswith('discovery-') else s
               for s in catalog['sources']]
    known = {catalog_url(s['url']) for s in sources}
    for row in candidates:
        key = catalog_url(row['url'])
        if (row['status']=='confirmed' and row['method']=='publisher_channel_link' and key not in known and
                profile(row['url'])==('telegram',row['url']) and row.get('readable_publications') and
                re.fullmatch(re.escape(row['url'])+r'/\d+',row.get('sample_permalink',''),re.I)):
            known.add(key)
            sources.append({'id':row['id'],'platform':'telegram','url':row['url'],'enabled':True,
                            'name':urlsplit(row['url']).path.strip('/'),'category':row['category'],
                            'origin':'dsns' if category(row['evidence_url'])=='dsns' else 'general',
                            'references':[{'kind':'discovered_public_channel','url':row['evidence_url'],
                                           'verified_at':row['checked_at']}]})
            continue
        if row['status']=='approved' and key and key not in known:
            known.add(key)
            sources.append({'id':row['id'],'platform':row['platform'],'url':row['url'],'name':row['name'],
                            'enabled':True,'category':row['category'],'origin':'dsns' if row['category']=='dsns' else 'general',
                            'references':[{'kind':'reviewed_public_source','url':row['review_evidence'],
                                           'sample_permalink':row['sample_permalink']}]})
            continue
        if (key in known or row['status'] != 'confirmed' or row['method'] != 'publisher_feed' or
                not key or key[0] not in ('rss','mailru') or row['platform'] != key[0] or
                not same_site(row['url'], row['evidence_url']) or
                not same_site(row.get('sample_permalink',''), row['url']) or not row.get('readable_publications')):
            continue
        known.add(key)
        kind = row['category']
        sources.append({'id':row['id'],'platform':key[0],'url':row['url'],'enabled':True,
                        'name':urlsplit(row['url']).hostname,'category':kind,
                        'origin':'dsns' if category(row['url'])=='dsns' else 'general',
                        'references':[{'kind':'discovered_public_feed','url':row['evidence_url'],
                                       'verified_at':row['checked_at']}]})
    return {**catalog, 'sources':sources}
