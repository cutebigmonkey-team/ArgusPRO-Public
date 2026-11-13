"""Public regression URLs for the November 2026 Air Alert incident."""
import json
from pathlib import Path
from bs4 import BeautifulSoup
from run import date, error_code, fetch

INCIDENT = ('air_alert_ua/338381','dsns_telegram/79375','mchs_official/35047')


def probe(identity):
    channel, number = identity.split('/')
    url = 'https://t.me/' + identity
    result = {'url':url,'published_at':None,'attempts':[]}
    for public_url in (url+'?embed=1','https://t.me/s/'+channel+'?before='+str(int(number)+1)):
        try:
            body, _ = fetch(public_url)
            card = BeautifulSoup(body,'html.parser').find(attrs={'data-post':identity})
            for link in card.select('a.tgme_widget_message_date[href]') if card else ():
                stamp = link.find('time', datetime=True)
                if stamp and link['href'].split('?')[0] == url and date(stamp['datetime']):
                    result.update(published_at=date(stamp['datetime']), verified_page=public_url)
                    return result
            result['attempts'].append('timestamp_not_exposed')
        except Exception as exc:
            status=error_code(exc)
            result['attempts'].append(status)
            if status in ('http_401','http_403','http_429'): break
    return result


if __name__=='__main__':
    rows=[probe(k) for k in INCIDENT]
    from crawl_common import CrawlState
    from news_sitemap import declared, children, document, rows as metadata
    catalog=json.loads(Path(__file__).with_name('sources.json').read_bytes())['sources']
    source=next(s for s in catalog if s['url']=='https://fixture.invalid/source/39')
    crawl=CrawlState(source,100,4)
    news={'source_id':source['id'],'target':'https://www.kommersant.ru/doc/9078670'}
    try:
        crawl.robots_allowed(source['url'])
        url=declared(crawl)
        news['sitemap']=url
        if url:
            body,_=crawl.read(url)
            news['root_tag']=document(body).tag
            nested=children(body,crawl)
            news['children']=nested[:3]
            if nested:body,_=crawl.read(nested[0])
            stories,total=metadata(body,crawl)
            news.update(raw=total,verified=len(stories),
                        target_matches=[{'url':p['post_url'],'published_at':p['published_at']} for p in stories
                                        if p['post_url']==news['target']])
    except Exception as exc:news['sitemap_status']=error_code(exc)
    try:
        if crawl.stopped: raise ValueError('source_access_restricted')
        body,_=crawl.read(news['target'])
        from web_pages import structured_rows
        news['article_dates']=[r['published_at'] for r in structured_rows(body,news['target']) if r['url']==news['target']]
    except Exception as exc:news['article_status']=error_code(exc)
    news['requests']=crawl.events
    Path('incident-public.json').write_text(json.dumps({'telegram':rows,'catalog_article':news},indent=2),encoding='utf-8')
    print(json.dumps(rows))
    if not all(r['published_at'] for r in rows):
        raise SystemExit('A public incident timestamp could not be confirmed')
