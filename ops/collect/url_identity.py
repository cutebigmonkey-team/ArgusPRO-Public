"""Recognize the conventional www alias without accepting arbitrary subdomains."""
import hashlib
import re
from urllib.parse import unquote_plus,urlsplit,urlunsplit,urljoin


def clean_permalink(url):
    parts=urlsplit(url)
    query=[]
    for token in parts.query.split('&'):
        name=unquote_plus(token.partition('=')[0]).lower()
        if token and not name.startswith('utm_') and name not in ('fbclid','gclid','igshid'):
            query.append(token)
    return urlunsplit((parts.scheme.lower(),parts.netloc.lower(),parts.path,'&'.join(query),''))


def identity_keys(row):
    key=row.get('canonical_key') or row['id']
    keys=[key]
    if row['platform']=='facebook' and str(row.get('facebook_story_id','')).isdigit():
        keys.append('facebook:'+str(row['facebook_story_id']))
    elif row['platform'] in ('rss','mailru'):
        url=clean_permalink(row.get('post_url') or row['url'])
        keys.append(row['platform']+':'+hashlib.sha256(url.encode()).hexdigest())
    aliases=row.get('identity_aliases',[])
    if isinstance(aliases,list) and row['platform']=='facebook':
        keys.extend(k for k in aliases[:100] if isinstance(k,str) and re.fullmatch(r'facebook:(?:\d+|pfbid[A-Za-z0-9]+)',k))
    if isinstance(aliases,list) and row['platform'] in ('rss','mailru'):
        keys.extend(k for k in aliases[:100] if isinstance(k,str) and re.fullmatch(row['platform']+r':[0-9a-f]{64}',k))
    return list(dict.fromkeys(keys))


def archive_identities(rows):
    # Keep already exposed archive IDs stable. Exact IDs take priority in legacy archives.
    aliases={r['id']:r['id'] for r in rows}
    for row in rows:
        for key in identity_keys(row):aliases.setdefault(key,row['id'])
    return aliases


def url_ok(url):
    if not isinstance(url,str):
        return False
    try:
        u=urlsplit(url)
        return u.scheme in ('https','http') and bool(u.hostname) and not u.username and not u.password and u.port in (None,80,443)
    except (ValueError,TypeError):
        return False


def same_site(left,right):
    if not url_ok(left) or not url_ok(right):return False
    return urlsplit(left).hostname.removeprefix('www.')==urlsplit(right).hostname.removeprefix('www.')


def article_identity(candidate,page_url):
    target=urljoin(page_url,candidate)
    if not same_site(target,page_url):return None
    page=urlsplit(page_url);identity=urlsplit(target)
    # Canonical metadata does not require another request. Keep the catalog host
    # in the identity so www metadata does not create duplicate records.
    return urlunsplit((page.scheme,page.netloc,identity.path,identity.query,''))
