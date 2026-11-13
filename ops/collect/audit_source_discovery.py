"""Read-only public discovery sample. No Drive credentials, catalog writes or hosted-run impersonation."""
import argparse
from datetime import datetime
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
from math import ceil
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from crawl_common import CrawlState
from run import error_code, feed_posts, now, telegram_html
from source_discovery import merge_candidates, scan, enroll
from url_identity import clean_permalink, same_site


def compare(source):
    """Enumerate publisher identifiers independently from the production parser, on identical bytes."""
    url = source['url']
    if source['platform'] == 'telegram':
        channel = urlsplit(url).path.strip('/').removeprefix('s/')
        url = 'https://t.me/s/' + channel
    reader = CrawlState({'url':url,'platform':'rss'}, 10000, 1)
    body, final = reader.read(url)
    if source['platform'] == 'telegram':
        if urlsplit(final).path.rstrip('/') != urlsplit(url).path:
            raise ValueError('unexpected_telegram_redirect')
        soup = BeautifulSoup(body, 'html.parser')
        expected = {'telegram:'+node['data-post'].lower() for node in soup.select('[data-post]')
                    if node['data-post'].lower().startswith(channel.lower()+'/') and
                    node['data-post'].split('/',1)[1].isdigit()}
        captured = {p['canonical_key'] for p in telegram_html(source, 10000, body)}
    else:
        root = ET.fromstring(body)
        expected = set()
        for node in root.iter():
            name = node.tag.split('}')[-1]
            if name not in ('entry','item'):
                continue
            for link in node:
                if link.tag.split('}')[-1] != 'link' or link.get('rel','alternate') != 'alternate':
                    continue
                href = link.get('href') or link.text
                if href and same_site(urljoin(final,href),final):
                    expected.add(clean_permalink(urljoin(final,href)).split('#')[0])
                    break
        captured = {p['post_url'].split('#')[0] for p in feed_posts(source, body, 10000, base_url=final)
                    if same_site(p['post_url'],final)}
    return {'denominator':len(expected), 'matched':len(expected & captured),
            'missing':sorted(expected-captured),'extra':len(captured-expected)}


def platform_check(source):
    platform, url = source['platform'], source['url']
    result = {'platform':platform,'source_id':source['id'],'url':url}
    try:
        if platform in ('rss','mailru'):
            from feed_history import collect
            rows,status,attempts,_ = collect(source,{},max_pages=2,seconds=30)
            result.update(status=status,posts=len(rows),reader=attempts[0]['tool'])
        elif platform=='telegram':
            sample=compare(source)
            result.update(status='ok' if sample['matched'] else 'no_readable_cards',posts=sample['matched'],**sample)
        elif platform=='youtube':
            from youtube_feed import collect
            reader=CrawlState({'platform':'rss','url':url},100,4)
            rows=collect(source,100,reader.read)
            result.update(status='ok' if rows else 'empty_feed',posts=len(rows),reader='youtube_feed')
        else:
            reader=CrawlState({'platform':'rss','url':url},100,1)
            body,final=reader.read(url)
            if platform=='rutube':
                from web_pages import article_rows
                rows=article_rows(body,final)
                result.update(status='public_metadata' if rows else 'no_article_metadata',metadata_rows=len(rows))
            else:
                result.update(status='public_html_requires_platform_adapter',bytes=len(body))
    except Exception as exc:
        result['status']=error_code(exc)
    return result


def observe(sample, polls):
    """Exclude the baseline head and date only keys first seen on a later successful poll."""
    source=next(r for r in sample['candidates'] if r['status']=='confirmed' and r['platform']=='rss')
    reader=CrawlState(source,10000,polls)
    seen=set()
    checks=[]
    observations=[]
    for index in range(polls):
        if index:
            time.sleep(60)
        try:
            body,final=reader.read(source['url'])
            stamp=now()
            rows=[p for p in feed_posts(source,body,10000,base_url=final) if same_site(p['post_url'],final)]
            if not rows:
                raise ValueError('empty_or_unreadable_feed')
        except Exception as exc:
            checks.append({'at':now(),'status':error_code(exc)})
            break
        fresh=[p for p in rows if p['canonical_key'] not in seen]
        checks.append({'at':stamp,'status':'ok','head':len(rows),'new_after_baseline':len(fresh) if index else None})
        seen.update(p['canonical_key'] for p in fresh)
        if index:
            for p in fresh:
                if p['published_at'] and not p.get('published_at_is_estimated'):
                    delay=(datetime.fromisoformat(stamp)-datetime.fromisoformat(p['published_at'])).total_seconds()
                    if delay>=0:
                        observations.append({'url':p['post_url'],'published_at':p['published_at'],'seen_at':stamp,'seconds':delay})
    values=sorted(p['seconds'] for p in observations)
    return {'url':source['url'],'checks':checks,'observations':observations,'n':len(values),
            'seconds':{f'p{p}':values[ceil(p*len(values)/100)-1] if values else None for p in (50,90,99)}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--sites', type=int, default=4)
    parser.add_argument('--cursor', default='')
    parser.add_argument('--sample', action='append', default=[], help='Configured Telegram source or confirmed feed URL')
    parser.add_argument('--platform-checks', action='store_true', help='Read one configured source on each requested platform')
    parser.add_argument('--observe-sample', type=Path, help='Follow the first confirmed RSS feed from a previous sample for four one-minute polls')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.observe_sample:
        result=observe(json.loads(args.observe_sample.read_bytes()),4)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'n':result['n'],'seconds':result['seconds']}))
        return
    if not 1 <= args.sites <= 32:
        parser.error('sites must be between 1 and 32')
    catalog = json.loads(Path(__file__).with_name('sources.json').read_bytes())['sources']
    rows, cursor, attempts = scan(catalog,args.cursor,budget=args.sites,seconds=180)
    rows = merge_candidates([],rows)
    sources = {r['url']:r for r in catalog if r['platform'] in ('rss','mailru','telegram')}
    confirmed = [r for r in rows if r['status']=='confirmed']
    sources.update({r['url']:r for r in confirmed})
    samples = []
    for url in dict.fromkeys(args.sample+[r['url'] for r in confirmed[:3]]):
        source = sources[url]
        try:
            samples.append({'url':url,'status':'ok',**compare(source)})
        except Exception as exc:
            samples.append({'url':url,'status':error_code(exc)})
    report = {'finished_at':now(),'configured':len(catalog),'cursor':cursor,'attempts':attempts,
              'candidates':rows,'counts':dict(Counter(r['status'] for r in rows)), 'samples':samples}
    active=enroll({'sources':catalog},confirmed)['sources'][len(catalog):]
    report['feed_collection']=[platform_check(s) for s in active]
    if args.platform_checks:
        selected={}
        for s in catalog:
            if s['platform']=='whatsapp' or not s.get('enabled',True):
                continue
            if s['platform']=='youtube' and '/channel/UC' not in s['url']:
                continue
            selected.setdefault(s['platform'],s)
        with ThreadPoolExecutor(max_workers=4) as pool:
            report['platform_checks']=list(pool.map(platform_check,selected.values()))
    report['finished_at']=now()
    args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='candidates'},ensure_ascii=True))


if __name__=='__main__':
    main()
