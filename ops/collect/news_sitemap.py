"""Read publisher-provided news metadata only from declared, same-site sitemaps."""
from urllib.parse import urlsplit
import re
import xml.etree.ElementTree as ET

from article_enrichment import row_post
from run import date

NS={'sm':'http://www.sitemaps.org/schemas/sitemap/0.9','news':'http://www.google.com/schemas/sitemap-news/0.9'}


def declared(crawl):
    robots=crawl.robots.get(crawl.origin.scheme+'://'+crawl.origin.netloc)
    if not robots:return None
    return next((u for u in robots.site_maps() or [] if crawl.allowed(u) and
                 re.search(r'news[^/]*\.xml$',urlsplit(u).path,re.I)),None)


def document(body):
    if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise ValueError('invalid_news_sitemap')
    try:root=ET.fromstring(body)
    except ET.ParseError as exc:raise ValueError('invalid_news_sitemap') from exc
    return root


def section_index(body,crawl):
    root=document(body)
    if root.tag!='{'+NS['sm']+'}sitemapindex':return {}
    entries=[(n.findtext('sm:lastmod','',NS),n.findtext('sm:loc','',NS)) for n in root.findall('sm:sitemap',NS)]
    return {url:stamp for stamp,url in sorted(entries,reverse=True) if crawl.allowed(url)}


def rows(body,crawl,candidates=None):
    root=document(body)
    if root.tag!='{'+NS['sm']+'}urlset':raise ValueError('invalid_news_sitemap')
    items={}; inputs=0
    for item in root.findall('sm:url',NS):
        article=item.find('news:news',NS)
        if article is None:continue
        inputs+=1
        url=(item.findtext('sm:loc','',NS) or '').strip()
        title=(article.findtext('news:title','',NS) or '').strip()
        if candidates is not None and title and crawl.allowed(url) and urlsplit(url).path not in ('','/'):
            from candidate_audit import publisher_candidate
            candidates.append(publisher_candidate(crawl.source['platform'],url,date(article.findtext('news:publication_date',None,NS))))
        if not title or not crawl.allowed(url) or urlsplit(url).path in ('','/'):
            from candidate_audit import reject
            reject('missing_news_title' if not title else 'invalid_news_permalink',url)
            continue
        post=row_post(crawl.source,{'url':url,'text':title,'published_at':date(article.findtext('news:publication_date',None,NS)),
                                   'capture_mode':'public_news_sitemap'})
        post['text_may_be_truncated']=True
        items[post['canonical_key']]=post
    return list(items.values()),inputs


def collect(crawl, url, previous, cutoff, *, deadline=float('inf')):
    import time
    from run import error_code, now
    from source_health import instant
    body,_=crawl.read(url)
    index=section_index(body,crawl)
    links=sorted(index)
    cursor=previous.get('sitemap_next')
    start=links.index(cursor) if cursor in links else 0
    rotation=links[start:]+links[:start]
    known={url:v for url,v in previous.get('sitemap_sections',{}).items() if url in index}
    # The first request advances the full sweep. Spare slots revisit sections
    # with a changed publisher timestamp or the most recently verified news.
    changed=[url for url in index if url in known and index[url] and index[url]!=known[url].get('modified')]
    productive=sorted((url for url in known if known[url].get('latest')),key=lambda u:known[u]['latest'],reverse=True)
    priority=[u for u in dict.fromkeys(changed+productive) if u not in rotation[:1]][:min(3,(crawl.max_pages-1)//3)]
    targets=list(dict.fromkeys(rotation[:1]+priority+rotation[1:]))[:max(0,crawl.max_pages-max(1,crawl.pages))]
    processed=set()
    items={};inputs=0;status='ok'; samples=[]
    for target in (targets if links else [url]):
        try:
            if items and (time.monotonic()>=deadline or len(items)>=crawl.limit): break
            data,_=crawl.read(target) if links else (body,url)
            from candidate_audit import sample_candidates
            candidates=[]
            posts,count=rows(data,crawl,candidates)
            samples.extend(sample_candidates(candidates))
            inputs+=count
            from candidate_audit import reject
            for p in posts:
                if p['published_at'] and instant(p['published_at'])<cutoff:
                    reject('before_publication_window',p['canonical_key'])
                else:
                    items[p['canonical_key']]=p
            if links:
                processed.add(target)
                latest = max((p['published_at'] for p in posts if p.get('published_at')),key=instant,default=None)
                known[target]={'modified':index[target],'latest':latest}
                cursor=next((u for u in rotation if u not in processed),rotation[0])
        except Exception as exc:
            status=error_code(exc)
            if not items:raise
            break
    if status=='ok' and not items:status='no_new_posts' if inputs else 'unverified_markup'
    update={'url':crawl.source['url'],'sitemap_next':cursor,'updated_at':now()} if inputs or (links and cursor) else None
    if update and len(index)<=2048:
        update['sitemap_sections']=known
    return list(items.values()),status,[{'tool':'publisher_news_sitemap','posts':len(items),'raw_rows':inputs,
           'status':status,'publisher_sample':samples,'rejected_rows':{k:v for k,v in crawl.exclusions.items() if k!='raw_input' and v},'index_sections':len(links),'sections_checked':crawl.pages-1 if links else 1}],update
