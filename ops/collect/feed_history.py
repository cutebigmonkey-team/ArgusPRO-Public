"""Refresh the head and resume same-site RSS/Atom continuations after archive acknowledgement."""
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

import feedparser
import time

from crawl_common import CrawlState
from run import error_code, feed_entries, now
from source_health import instant


def backfill_article(source):
    from article_enrichment import row_post
    from url_identity import clean_permalink
    from web_pages import article_rows
    crawl=CrawlState(source,100,1)
    target=clean_permalink(source['backfill_url'])
    if not crawl.allowed(target):raise ValueError('cross_origin_redirect')
    body,final=crawl.read(target)
    def resource(url):
        from urllib.parse import urlsplit
        u=urlsplit(clean_permalink(url))
        return (u.hostname.removeprefix('www.'),u.path.rstrip('/'),u.query)
    if resource(final)!=resource(target):raise ValueError('unexpected_article_redirect')
    posts=[row_post(source,r) for r in article_rows(body,final) if resource(r['url'])==resource(target)]
    status='ok' if posts else 'unverified_markup'
    return posts,status,[{'tool':'public_article_backfill','status':status,'posts':len(posts)}],None


def listing_and_sitemap(crawl, listing, sitemap, previous, cutoff, deadline):
    """Use one origin/robots/request budget for the public headline list and the sitemap."""
    from article_enrichment import row_post
    from candidate_audit import reject
    from news_sitemap import collect as sitemap_posts
    from web_pages import article_links, article_rows
    from url_identity import clean_permalink
    from urllib.parse import urlsplit
    def resource(url):
        parsed=urlsplit(clean_permalink(url))
        return (parsed.hostname.removeprefix('www.'),parsed.path.rstrip('/'),parsed.query)
    restored=previous.get('listing',{})
    queue=list(dict.fromkeys(u for u in restored.get('pending',[]) if crawl.allowed(u)))
    prior=set(restored.get('head',[]))
    found={}
    attempt={'tool':'publisher_headlines','status':'ok','fetched_details':0}
    update=dict(previous)
    head=None
    try:
        body,final=crawl.read(listing)
        links=article_links(body,final,None)
        additions=[u for u in links if u not in prior and u not in queue]
        if len(links)>2048 or len(queue)+len(additions)>2048:
            raise ValueError('feed_continuation_overflow')
        head=links
        queue=additions[:1]+queue[:1]+additions[1:]+queue[1:]
        attempt['discovered_links']=len(head)
        for url in queue[:min(3,max(0,crawl.max_pages-4))]:
            if time.monotonic()>=deadline:break
            try:
                body,final=crawl.read(url)
            except Exception as exc:
                if error_code(exc) in ('http_404','http_410'):
                    reject(error_code(exc),url)
                    queue.remove(url)
                    continue
                raise
            attempt['fetched_details']+=1
            verified=[row_post(crawl.source,r) for r in article_rows(body,final) if resource(r['url'])==resource(url)]
            queue.remove(url)
            if not verified:
                reject('unverified_article_detail',url)
                queue.append(url)
            for row in verified:
                if row.get('published_at') and instant(row['published_at'])<cutoff:
                    reject('before_publication_window',row['canonical_key'])
                else:
                    # Same article, exact path and query on the configured www/apex pair.
                    from hashlib import sha256
                    original=urlsplit(crawl.source['url'])
                    alias=urlsplit(row['post_url'])._replace(scheme=original.scheme,netloc=original.netloc).geturl()
                    row['identity_aliases']=[crawl.source['platform']+':'+sha256(clean_permalink(alias).encode()).hexdigest()]
                    found[row['canonical_key']]=row
    except Exception as exc:
        attempt['status']=error_code(exc)
    if head is not None:update['listing']={'head':head,'pending':queue}
    attempt.update(remaining_links=len(queue),posts=len(found))
    attempts=[attempt]
    status=attempt['status']
    blocked=status.startswith('robots_') or status in ('challenge_required','login_required','http_401','http_403','http_429','http_503')
    if not blocked and time.monotonic()<deadline and crawl.pages<crawl.max_pages:
        try:
            rows,map_status,extra,checkpoint=sitemap_posts(crawl,sitemap,previous,cutoff,deadline=deadline)
            if status=='ok':status=map_status
            for r in rows:found.setdefault(r['canonical_key'],r)
            attempts.extend(extra)
            if checkpoint:update.update(checkpoint)
            if head is not None:
                covered={resource(r['post_url']) for r in rows}
                update['listing']['pending']=[u for u in queue if resource(u) not in covered]
        except Exception as exc:status=error_code(exc)
    if head is not None or found:update.update(url=crawl.source['url'],updated_at=now())
    else:update=None
    return list(found.values()),('ok' if found and status in ('ok','no_new_posts') else status),attempts,update


def collect(source, previous, *, window_hours=48, max_pages=3, max_posts=1000, seconds=45):
    if source.get('backfill_url'):return backfill_article(source)
    crawl = CrawlState(source, max_posts, max_pages)
    deadline=time.monotonic()+seconds
    pending = list(previous.get('pending', []))
    visited = set()
    items = {}
    pages = []
    samples = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours) if window_hours else datetime.min.replace(tzinfo=timezone.utc)
    status = 'ok'
    head = source['url']
    validators=previous.get('http_validators',{}) if previous.get('url')==head else {}
    conditional={}
    for name,header in (('etag','If-None-Match'),('last-modified','If-Modified-Since')):
        value=validators.get(name)
        if isinstance(value,str) and len(value)<=1024 and '\r' not in value and '\n' not in value:conditional[header]=value
    if conditional:crawl.conditional[head]=conditional
    from news_sitemap import declared,collect as sitemap_posts
    crawl.robots_allowed(head)
    explicit_feed = any(ref.get('kind') == 'discovered_public_feed' for ref in source.get('references', []))
    from import_sheet import is_operator_source
    from urllib.parse import urlsplit
    requested_section = is_operator_source(source) and bool(urlsplit(head).path.strip('/'))
    sitemap=None if explicit_feed or requested_section else declared(crawl)
    listing=next((r.get('url') for r in source.get('references',[]) if r.get('kind')=='publisher_news_listing'),None)
    if sitemap and listing and crawl.allowed(listing) and max_pages>=5:
        return listing_and_sitemap(crawl,listing,sitemap,previous,cutoff,deadline)
    if sitemap:
        try:
            result=sitemap_posts(crawl,sitemap,previous,cutoff,deadline=deadline)
            # A valid but old sitemap need not describe the current public feed.
            # Try the configured page/declared alternate only while its robots policy allows it.
            if result[0] or result[1] not in ('no_new_posts','unverified_markup') or crawl.pages >= crawl.max_pages:
                return result
            previous = {**previous, **(result[3] or {})}
        except Exception as exc:
            if crawl.stopped or error_code(exc) in ('http_401','http_403','http_429'):raise
    for index in range(crawl.max_pages):
        if index and (time.monotonic()>=deadline or len(items)>=max_posts):
            break
        if index and not pending:
            break
        url = pending[0] if index else head
        if url in visited or not crawl.allowed(url):
            status = 'invalid_feed_continuation'
            break
        try:
            body, final = crawl.read(url)
            if url in crawl.not_modified:
                pages.append({'rows':0,'in_window':0,'not_modified':True})
                visited.add(url)
                continue  # pending backfill pages remain eligible after a 304 head
            from feed_formats import parse
            parsed = parse(body)
            if not parsed.version:
                # Use the existing same-site HTML article and alternate-feed discovery.
                crawl.items.clear()
                links = crawl.consume(body, final, checkpoint_links=True)
                rows = list(crawl.items.values())
                more = links
            else:
                from candidate_audit import publisher_candidate, sample_candidates
                from run import feed_date
                candidates=[]
                for entry in parsed.entries:
                    link=urljoin(final,entry.get('link') or '')
                    if entry.get('link') and crawl.allowed(link):
                        candidates.append(publisher_candidate(source['platform'],link,feed_date(entry)))
                samples.extend(sample_candidates(candidates))
                rows = feed_entries(source, parsed.entries, len(parsed.entries), base_url=final)
                from candidate_audit import reject
                for row in rows:
                    if not crawl.allowed(row['post_url']):
                        reject('off_site_permalink',row['canonical_key'])
                rows = [p for p in rows if crawl.allowed(p['post_url'])]
                if not rows and (parsed.bozo or parsed.entries):
                    status = 'unverified_markup'
                    break
                more = [urljoin(final, link['href']) for link in parsed.feed.get('links', [])
                        if link.get('rel') == 'next' and link.get('href')][:1]
            fresh = [p for p in rows if not p.get('published_at') or instant(p['published_at']) >= cutoff]
            items.update((p['canonical_key'], p) for p in fresh)
            from candidate_audit import reject
            for row in rows:
                if row.get('published_at') and instant(row['published_at'])<cutoff:
                    reject('before_publication_window',row['canonical_key'])
            pages.append({'rows':len(rows), 'in_window':len(fresh)})
            if not index and parsed.version:validators=dict(crawl.response_headers.get(url,{}))
            visited.add(url)
            if index: pending.pop(0)
            # A pinned or out-of-order page can contain only older articles.
            # Keep the publisher's continuation for the next budgeted pass.
            queued = set(pending)
            for link in more:
                if crawl.allowed(link) and link not in visited and link not in queued:
                    pending.append(link)
                    queued.add(link)
            if len(pending) > 2048:
                # Keep the last committed frontier and retry. Never truncate unseen work.
                return list(items.values()), 'feed_continuation_overflow', [
                    {'tool':'public_feed_history','status':'feed_continuation_overflow','posts':len(items)}], None
            if not parsed.version and not rows and not more:
                status = 'unverified_markup'
                if index: pending.append(url)
                break
        except Exception as error:
            status = error_code(error)
            break
    if status == 'ok' and not items:
        status = 'no_new_posts'
    checkpoint = {'url':head, 'pending':pending, 'updated_at':now()} if pages else None
    if checkpoint and validators:checkpoint['http_validators']=validators
    if checkpoint and previous.get('sitemap_next'):
        checkpoint['sitemap_next'] = previous['sitemap_next']
    attempt = {'tool':'public_feed_history','status':status,'posts':len(items),'pages':pages,
               'raw_rows':crawl.exclusions['raw_input'],'publisher_sample':samples,
               'rejected_rows':{k:v for k,v in crawl.exclusions.items() if k!='raw_input' and v},
               'remaining_pages':len(pending)}
    return list(items.values()), status, [attempt], checkpoint
