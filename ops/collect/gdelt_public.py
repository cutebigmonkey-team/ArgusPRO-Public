"""Public GDELT article discovery. seendate is NOT a publication timestamp."""
import hashlib
import ipaddress
import json
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
from urllib.robotparser import RobotFileParser
import urllib.error


def permitted(url,fetch):
    parsed=urlsplit(url);origin=urlunsplit((parsed.scheme,parsed.netloc,'','',''))
    try:
        body,final=fetch(origin+'/robots.txt')
        if urlsplit(final).hostname!=parsed.hostname:raise ValueError('robots_cross_origin')
        from crawl_common import robots_error
        text=body.decode('utf-8-sig','replace')
        failure=robots_error(final,text)
        if failure:raise ValueError(failure)
    except urllib.error.HTTPError as error:
        if error.code!=404:raise
        text=''
    parser=RobotFileParser();parser.parse(text.splitlines())
    if not parser.can_fetch('ArgusPRO',url):raise ValueError('robots_denied')


def collect(source,limit,fetch):
    from run import post
    from url_identity import url_ok,clean_permalink
    parsed=urlsplit(source['url'])
    if (parsed.scheme!='https' or parsed.hostname!='api.gdeltproject.org' or
            parsed.path!='/api/v2/doc/doc'):raise ValueError('invalid_gdelt_source')
    query=dict(parse_qsl(parsed.query))
    if any(key.lower() in {'token','access_token','api_key','session','cookie','auth','password'} for key in query):
        raise ValueError('invalid_gdelt_source')
    if not query.get('query'):raise ValueError('missing_gdelt_query')
    cap=min(250,max(1,limit))
    query.update(mode='artlist',format='json',maxrecords=str(cap),sort='datedesc')
    url=urlunsplit((parsed.scheme,parsed.netloc,parsed.path,urlencode(query),''))
    permitted(url,fetch)
    body,final=fetch(url)
    if urlsplit(final).hostname!=parsed.hostname:raise ValueError('unexpected_destination')
    data=json.loads(body)
    if not isinstance(data,dict) or not isinstance(data.get('articles'),list):raise ValueError('unverified_markup')
    items={}
    for row in data['articles'][:cap]:
        if not isinstance(row,dict) or not url_ok(row.get('url')) or not isinstance(row.get('title'),str):continue
        host=urlsplit(row['url']).hostname
        if host=='localhost' or host.endswith(('.local','.internal','.localhost')):continue
        try:
            if not ipaddress.ip_address(host).is_global:continue
        except ValueError:pass
        original=clean_permalink(row['url'])
        key='gdelt:'+hashlib.sha256(original.encode()).hexdigest()
        item=post(source,key,original,row['title'],None)
        item.update(capture_mode='gdelt_public_article',gdelt_seen_at=str(row.get('seendate') or '')[:40],
                    text_may_be_truncated=True)
        items[key]=item
    return list(items.values()),'partial_results' if len(data['articles'])>=cap else 'ok' if items else 'no_public_posts'
