"""Compare a clean RSSHub container and public gateways; no archive writes."""
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import feedparser
from run import safe_url, HOSTS, IDENTITIES, error_code, feed_date

HUBS=('http://127.0.0.1:1200','https://rsshub.app','https://rsshub.pseudoyu.com')
BRIDGES=('https://rss-bridge.org/bridge01/','https://rss-bridge.sans-nuage.fr/','https://rssbridge.flossboxin.org.in/')
MAX_BYTES=2*1024**2


def failure_reason(error):
    if not isinstance(error,urllib.error.HTTPError):return None
    try:body=error.read(100000).decode('utf-8','replace')
    except Exception:return None
    for pattern,code in ((r'ConfigNotFoundError|No valid Twitter token|TWITTER_AUTH_TOKEN|INSTAGRAM_COOKIE','gateway_auth_configuration'),
                         (r'not whitelisted|bridge.*not enabled|bridge.*not found','bridge_unavailable'),
                         (r'login_required|LoginRequired|login required','platform_login_required'),
                         (r'Too Many Requests|rate.limit','rate_limited'),
                         (r'captcha|verify you are human|Just a moment','challenge_page')):
        if re.search(pattern,body,re.I):return code
    return None


def candidates(body,source):
    """An RSS-Bridge error can itself be an RSS item: require original post IDs."""
    parsed=feedparser.parse(body);items={}
    for entry in parsed.entries[:100]:
        url=entry.get('link','');platform=source['platform']
        try:
            parts=urllib.parse.urlsplit(url)
            if parts.scheme!='https' or parts.hostname not in HOSTS[platform] or parts.username or parts.password:continue
            identity=re.search(IDENTITIES[platform],parts.path)
            if not identity:continue
            if platform=='x' and parts.path.split('/')[1].lower()!=urllib.parse.urlsplit(source['url']).path.strip('/').lower():continue
            published=feed_date(entry)
            items[identity[1]]={'url':url,'published_at':published}
        except (ValueError,TypeError,IndexError,OverflowError):continue
    return {'feed_entries':len(parsed.entries),'candidate_posts':len(items),'examples':list(items.values())[:3],
            'latest_published_at':max((p['published_at'] for p in items.values() if p['published_at']),default=None)}


def destination(url,local=False):
    if local:
        u=urllib.parse.urlsplit(url)
        if u.scheme!='http' or u.hostname!='127.0.0.1' or u.port!=1200 or u.username or u.password:raise ValueError('invalid_local_gateway')
    else:safe_url(url)


def read(url,local=False):
    destination(url,local)
    class Redirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,req,fp,code,msg,headers,newurl):
            destination(newurl,local)
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    req=urllib.request.Request(url,headers={'User-Agent':'ArgusPRO/1.0 RSS gateway probe','Accept':'application/atom+xml,application/rss+xml,application/xml,text/xml;q=0.9'})
    with urllib.request.build_opener(Redirects()).open(req,timeout=25) as response:
        body=response.read(MAX_BYTES+1)
        if len(body)>MAX_BYTES:raise ValueError('response_too_large')
        return body


def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':raise RuntimeError('Probe requires GitHub-hosted Actions')
    root=Path('collection/rss-probe');root.mkdir(parents=True,exist_ok=True)
    catalog=json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8'))['sources']
    requested={'https://fixture.invalid/source/1750','https://www.instagram.com/fixture584'}
    sources=[s for s in catalog if s['url'] in requested]
    if len(sources)!=2:raise ValueError('Probe sources missing from catalog')
    results=[]
    for _ in range(30):
        try:read(HUBS[0],True);break
        except Exception:time.sleep(2)
    for gateway in HUBS+BRIDGES:
        rate_limited=False
        for source in sources:
            handle=urllib.parse.urlsplit(source['url']).path.strip('/')
            if gateway in HUBS:
                route='/twitter/user/' if source['platform']=='x' else '/instagram/user/'
                url=gateway+route+urllib.parse.quote(handle,safe='')
            else:
                query={'action':'display','bridge':'Twitter' if source['platform']=='x' else 'Instagram',
                       'context':'By username' if source['platform']=='x' else 'Username','u':handle,'format':'Atom'}
                url=gateway+'?'+urllib.parse.urlencode(query)
            started=time.monotonic();record={'gateway':gateway,'platform':source['platform'],'source_url':source['url']}
            if rate_limited:
                record.update(status='deferred_gateway_rate_limit',candidate_posts=0,seconds=0);results.append(record);continue
            try:
                record.update(candidates(read(url,gateway==HUBS[0]),source))
                record['status']='candidates_found' if record['candidate_posts'] else 'no_verified_post_links'
            except Exception as error:
                record.update(status=error_code(error),candidate_posts=0,reason=failure_reason(error))
                rate_limited=record['status']=='http_429' or record.get('reason')=='rate_limited'
            record['seconds']=round(time.monotonic()-started,2);results.append(record)
            (root/'report.json').write_text(json.dumps({'run_id':os.environ.get('GITHUB_RUN_ID'),'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(record),flush=True)
            time.sleep(3)
    (root/'report.json').write_text(json.dumps({'run_id':os.environ.get('GITHUB_RUN_ID'),'results':results},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
