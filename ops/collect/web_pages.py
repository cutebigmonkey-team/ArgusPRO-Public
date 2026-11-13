"""Extract public article/video metadata; never turn a homepage into a post."""
import json
import re
from urllib.parse import urljoin,urlsplit
from bs4 import BeautifulSoup
from url_identity import article_identity,same_site

def documents(body):
    soup=BeautifulSoup(body,'html.parser');output=[]
    def walk(value,depth=0):
        if depth>10 or len(output)>=80:return
        if isinstance(value,list):
            for child in value[:100]:walk(child,depth+1)
        elif isinstance(value,dict):
            types=value.get('@type',[]);types=[types] if isinstance(types,str) else types
            if any(t in ('NewsArticle','Article','BlogPosting','VideoObject','LiveBlogPosting') for t in types):output.append(value)
            for key in ('@graph','itemListElement','item','mainEntity','subjectOf'):
                if key in value:walk(value[key],depth+1)
    for script in soup.select('script[type="application/ld+json"]')[:40]:
        try:walk(json.loads(script.string or script.get_text()))
        except (ValueError,TypeError):pass
    return soup,output

def structured_rows(body,url):
    soup,docs=documents(body);rows=[]
    for doc in docs:
        identity=doc.get('url') or doc.get('mainEntityOfPage') or url
        if isinstance(identity,dict):identity=identity.get('@id') or identity.get('url') or url
        if not isinstance(identity,str):continue
        identity=article_identity(identity,url)
        if not identity or urlsplit(identity).path in ('','/'):continue
        title=doc.get('headline') or doc.get('name');stamp=doc.get('datePublished') or doc.get('uploadDate')
        text=doc.get('articleBody') or doc.get('description') or ''
        if not doc.get('articleBody') and len(docs)==1:
            article=soup.find('article')
            paragraphs=article.select('p') if article else []
            if paragraphs:text='\n'.join(p.get_text(' ',strip=True) for p in paragraphs)
        images=doc.get('image') or doc.get('thumbnailUrl') or []
        images=[images] if isinstance(images,(str,dict)) else images
        images=[i.get('url','') if isinstance(i,dict) else i for i in images]
        from run import url_ok
        images=[urljoin(identity,i) for i in images if isinstance(i,str) and i and url_ok(urljoin(identity,i))]
        content='\n\n'.join(v for v in (title,text) if isinstance(v,str) and v.strip())
        if not content and not images:continue
        rows.append({'url':identity,'text':content,'published_at':stamp,'media':images,
                     'capture_mode':'structured_article' if doc.get('articleBody') else 'public_article_excerpt'})
    if rows:return rows
    # Many news sites expose OpenGraph/article metadata instead of JSON-LD.
    # The article type and its permalink establish identity independently of its date.
    def meta(key):
        node=soup.find('meta',attrs={'property':key}) or soup.find('meta',attrs={'name':key})
        return node.get('content','') if node else ''
    if meta('og:type') not in ('article','news'):return []
    stamp=meta('article:published_time') or meta('datePublished')
    if not stamp and urlsplit(url).hostname in ('gorod24.online','www.gorod24.online'):
        # Only the article header: other time elements belong to recommendations.
        stamp_node=soup.select_one('.head-single-news-head > .time > time[datetime]')
        if stamp_node and soup.select_one('.head-single-news-head h1'):stamp=stamp_node['datetime']
    title=meta('og:title')
    canonical=soup.find('link',rel='canonical')
    identity=article_identity(canonical.get('href') if canonical and canonical.get('href') else meta('og:url') or url,url)
    if not identity or urlsplit(identity).path in ('','/'):return []
    article=soup.find('article') or soup.select_one('[itemprop="articleBody"],.article-body,.news-content')
    paragraphs=[p.get_text(' ',strip=True) for p in article.select('p')] if article else []
    text='\n'.join(p for p in paragraphs if p) or meta('og:description') or meta('description')
    content='\n\n'.join(v for v in (title,text) if v)
    from run import url_ok
    image=urljoin(identity,meta('og:image')) if meta('og:image') else None
    media=[image] if image and url_ok(image) else []
    if not content and not media:return []
    return [{'url':identity,'text':content,'published_at':stamp or None,'media':media,
             'capture_mode':'public_article_excerpt'}]

def article_links(body,url,limit):
    soup,_=documents(body);links=[];seen=set()
    for anchor in soup.select('article a[href],main a[href],.news-list a[href],.news-item a[href],a[href]'):
        target=urljoin(url,anchor.get('href','')).split('#')[0];p=urlsplit(target)
        if not same_site(target,url) or target.rstrip('/')==url.rstrip('/') or target in seen:continue
        if not re.search(r'/(?:news|novini|novyny|article|articles|proisshestviya|novosti|politics|society)/.+|/\d{4}/\d{2}/\d{2}/.+|/\d{5,}(?:[-./]|$)',p.path):continue
        if len(anchor.get_text(' ',strip=True))<12:continue
        seen.add(target)
        links.append(target)
        if limit is not None and len(links)>=limit:break
    return links


def article_rows(body,url):
    """Use extraction libraries only after identifying an actual article."""
    from article_enrichment import enrich
    rows=structured_rows(body,url);dom_candidate=False
    soup,docs=documents(body)
    if not rows and not docs and len(soup.find_all('article'))==1:
        article=soup.find('article');title=article.find('h1');stamp=article.find('time',datetime=True)
        canonical=soup.find('link',rel='canonical')
        identity=article_identity(canonical.get('href') or url,url) if canonical else url
        if (title and identity
                and urlsplit(identity).path not in ('','/')):
            rows=[{'url':identity.split('#')[0],'text':title.get_text(' ',strip=True),
                   'published_at':stamp['datetime'] if stamp else None,'media':[],'capture_mode':'public_article_excerpt'}]
            dom_candidate=True
    # Never apply the full page's text to several separate JSON-LD articles.
    if len(rows)==1 and len(docs)<=1 and urlsplit(url).path not in ('','/'):
        rows=enrich(body,url,rows)
    if dom_candidate and (not rows or rows[0]['capture_mode']!='trafilatura_article'):return []
    if len(rows)==1:
        from social_links import outbound
        scope=soup.find('article') or soup.select_one('[itemprop="articleBody"],.article-body,.news-content') or soup.find('main')
        rows[0].update(outbound(str(scope) if scope else rows[0]['text'],url))
    return rows
