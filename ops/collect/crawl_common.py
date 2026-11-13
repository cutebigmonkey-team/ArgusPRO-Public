"""Shared origin, robots, request budget and record rules for both crawler engines."""
import time
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit,urljoin
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup

USER_AGENT='ArgusPRO/1.0 public-feed-reader'
STOP={'http_401','http_403','http_429','challenge_required','login_required','robots_denied'}


def robots_error(url,text):
    from browser_public import page_error
    # A valid Disallow path often contains "captcha". Classify the response
    # surface, then pass the original, unmodified directives to RobotFileParser.
    html=bool(re.search(r'<(?:!doctype\s+html|html|body|script)\b',text,re.I))
    prose='\n'.join(line for line in text.splitlines()
                    if not re.match(r'^\s*(?:#|[a-zA-Z][\w-]*\s*:)',line))
    if page_error(url,text if html else prose):return 'robots_challenge_required'
    return 'robots_invalid_content' if html else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


class CrawlState:
    def __init__(self,source,limit,max_pages):
        from run import source_valid
        if not source_valid(source) or source['platform'] not in ('rss','mailru'):raise ValueError('invalid_source')
        if not 1<=limit<=10000 or not 1<=max_pages<=200:raise ValueError('invalid_crawl_bounds')
        self.empty_feed=False
        from candidate_audit import start
        self.exclusions=start()
        self.source=source;self.limit=limit;self.max_pages=max_pages
        self.origin=urlsplit(source['url']);self.pages=0;self.items={};self.errors=[];self.events=[]
        self.scheduled={source['url']};self.pending=[];self.stopped=False;self.robots={};self.last_request=0.0
        self.conditional={};self.response_headers={};self.not_modified=set()

    def allowed(self,url):
        from url_identity import same_site
        return same_site(url,self.source['url'])

    def raw(self,url):
        from run import safe_url
        if not self.allowed(url):raise ValueError('cross_origin_redirect')
        safe_url(url)
        time.sleep(max(0,1-(time.monotonic()-self.last_request)))
        self.last_request=time.monotonic()
        request=urllib.request.Request(url,headers={'User-Agent':USER_AGENT,
            'Accept':'text/html,application/rss+xml,application/atom+xml,application/xml,text/xml,text/plain,*/*;q=0.5'})
        for name,value in self.conditional.get(url,{}).items():request.add_header(name,value)
        event={'url':url,'stage':'robots' if urlsplit(url).path=='/robots.txt' else 'page'}
        try:
            from http_pool import request as pooled_request
            headers={}
            body,final=pooled_request(url,dict(request.header_items()),10,response_headers=headers)
            self.response_headers[url]=headers
            event.update(status=200,bytes=len(body))
            return body,final
        except urllib.error.HTTPError as error:
            event['status']=error.code
            if error.code==304 and url in self.conditional:
                self.not_modified.add(url)
                return b'',url
            raise
        except Exception as error:
            from run import error_code
            event['error']=error_code(error);raise
        finally:self.events.append(event)

    def robots_allowed(self,url):
        from browser_public import page_error
        parts=urlsplit(url);base=parts.scheme+'://'+parts.netloc
        if base not in self.robots:
            parser=RobotFileParser()
            try:
                target=base+'/robots.txt'
                for _ in range(4):
                    try:
                        body,_=self.raw(target);break
                    except urllib.error.HTTPError as error:
                        if error.code not in (301,302,303,307,308):raise
                        target=urljoin(target,error.headers.get('Location',''))
                        if not self.allowed(target):raise ValueError('robots_cross_origin')
                else:raise ValueError('robots_redirect_limit')
                text=body.decode('utf-8-sig','replace')
                failure=robots_error(target,text)
                if failure:raise ValueError(failure)
                parser.parse(text.splitlines())
            except urllib.error.HTTPError as error:
                if error.code in (429,503) and (error.code==429 or error.headers.get('Retry-After')):
                    raise
                if error.code==404:parser.parse([])
                else:
                    failure=page_error(target,error.read(65536).decode('utf-8','replace'))
                    raise ValueError('robots_challenge_required' if failure else 'robots_http_'+str(error.code)) from None
            self.robots[base]=parser
        if not self.robots[base].can_fetch(USER_AGENT,url):raise ValueError('robots_denied')
        delay=self.robots[base].crawl_delay(USER_AGENT)
        if delay and delay>30:raise ValueError('robots_delay_exceeds_budget')
        if delay:time.sleep(max(0,delay-(time.monotonic()-self.last_request)))

    def read(self,url):
        from run import error_code
        from browser_public import page_error
        if self.stopped or self.pages>=self.max_pages:raise ValueError('crawl_budget_reached')
        self.pages+=1;current=url
        try:
            for redirect in range(4):
                self.robots_allowed(current)
                for attempt in range(2):
                    try:
                        body,final=self.raw(current)
                        failure=page_error(final,body.decode('utf-8','replace'))
                        if failure:raise ValueError(failure)
                        return body,final
                    except urllib.error.HTTPError as error:
                        if error.code in (301,302,303,307,308):
                            current=urljoin(current,error.headers.get('Location',''))
                            if not self.allowed(current):raise ValueError('cross_origin_redirect')
                            break
                        if error.code not in (502,503,504) or attempt or (error.headers and error.headers.get('Retry-After')):raise
                    except (urllib.error.URLError,TimeoutError):
                        if attempt:raise
                else:continue
            raise ValueError('redirect_limit')
        except Exception as error:
            code=error_code(error);self.errors.append(code)
            if code in STOP or code.startswith('robots_'):self.stopped=True
            raise

    def consume(self,body,url, *, checkpoint_links=False):
        from run import feed_posts
        from web_pages import article_rows,article_links
        from article_enrichment import row_post
        posts=feed_posts(self.source,body,self.limit,base_url=url);is_feed=bool(posts)
        if not posts:
            rows=article_rows(body,url)
            self.exclusions['raw_input']+=len(rows)
            posts=[row_post(self.source,r) for r in rows]
        for item in posts:
            key=item['canonical_key']
            if not self.allowed(item['post_url']):
                self.exclusions['off_site_permalink']+=1
                continue
            previous=self.items.get(key)
            if previous:
                if len(item['text'])>len(previous['text']):
                    if not item.get('published_at'):item['published_at']=previous.get('published_at')
                    self.items[key]=item
            elif checkpoint_links or len(self.items)<self.limit:self.items[key]=item
        if self.stopped:return []
        soup=BeautifulSoup(body,'html.parser')
        urls=[p['post_url'] for p in posts] if is_feed else []
        if is_feed and len(self.items)<self.limit:
            import feedparser
            more=[urljoin(url,link['href']) for link in feedparser.parse(body).feed.get('links',[])
                  if link.get('rel')=='next' and link.get('href')]
            urls=more[:1]+urls
        if not posts and not is_feed:
            import feedparser
            parsed = feedparser.parse(body)
            self.empty_feed |= bool(parsed.version and not parsed.bozo and not parsed.entries)
        if not is_feed and (checkpoint_links or len(self.items)<self.limit):
            urls+=[urljoin(url,n['href']) for n in soup.select('link[rel="alternate"][href]')[:4]
                   if n.get('type') in ('application/rss+xml','application/atom+xml')]
            urls+=article_links(body,url,None if checkpoint_links else self.max_pages)
            # Semantic article cards also cover nonstandard article paths.
            anchors=soup.select('article a[href]')
            urls += [urljoin(url,n['href']) for n in (anchors if checkpoint_links else anchors[:40])
                     if len(n.get_text(' ',strip=True))>=12]
        pending=[]
        for candidate in urls:
            candidate=candidate.split('#')[0]
            if self.allowed(candidate) and urlsplit(candidate).path not in ('','/'):
                pending.append(candidate)
        # Enqueue one at a time: newly discovered feed articles take precedence
        # over alternate feeds, without reserving the entire budget on the homepage.
        if checkpoint_links:
            return list(dict.fromkeys(pending))
        self.pending=list(dict.fromkeys(pending+self.pending))[:20]
        return self.next_urls()

    def next_urls(self):
        if self.stopped or len(self.scheduled)>=self.max_pages:return []
        while self.pending:
            candidate=self.pending.pop(0)
            if candidate in self.scheduled:continue
            if len(self.items)>=self.limit and candidate not in {p['post_url'] for p in self.items.values()}:continue
            self.scheduled.add(candidate)
            return [candidate]
        return []

    def result(self):
        return {'posts':list(self.items.values()),'pages':self.pages,'errors':self.errors,'requests':self.events[:32],
                'raw_rows':self.exclusions['raw_input'],
                'rejected_rows':{k:v for k,v in self.exclusions.items() if k!='raw_input'},
                'status':'ok' if self.items else (self.errors[-1] if self.errors else 'no_public_posts' if self.empty_feed else 'unverified_markup')}
