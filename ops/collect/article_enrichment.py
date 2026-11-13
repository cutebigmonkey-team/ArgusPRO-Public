"""Offline extraction. No LLM calls, browser launch or network requests."""
import html
import os


def enrich(body,url,rows):
    try:
        from trafilatura import extract
        from trafilatura.deduplication import LRUCache
    except ImportError:
        return rows
    from browser_public import page_error
    if page_error(url,str(body)[:10000]):return []
    row=dict(rows[0]);row['extraction_tools']=['structured_metadata']
    try:
        text=extract(body,url=url,include_comments=False,include_tables=True,
                     favor_precision=True,deduplicate=LRUCache(maxsize=1024))
        title=row['text'].split('\n',1)[0]
        previous=row['text'].partition('\n\n')[2]
        if text and len(text.strip())>=max(120,len(previous)):
            row['text']=title+'\n\n'+text.strip()
            row['capture_mode']='trafilatura_article'
            row['extraction_tools'].append('trafilatura')
    except Exception:
        row['extraction_warning']='trafilatura_failed'
    if os.environ.get('ARGUS_ENHANCED_COLLECTION')=='1':
        try:
            from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
            # Convert only the accepted article text: no navigation, scripts or unrelated stories.
            parts=row['text'].split('\n\n',1)
            accepted='<h1>'+html.escape(parts[0])+'</h1>'+''.join(
                '<p>'+html.escape(p)+'</p>' for p in (parts[1] if len(parts)>1 else '').split('\n') if p)
            result=DefaultMarkdownGenerator(options={'body_width':0}).generate_markdown(accepted,base_url=url)
            row['markdown']=result.raw_markdown[:100000]
            row['extraction_tools'].append('crawl4ai_markdown')
        except Exception:
            row['markdown_warning']='crawl4ai_failed'
    return [row]


def row_post(source,row):
    import hashlib
    import datetime as dt
    from run import post,plain
    from url_identity import clean_permalink
    url=clean_permalink(row['url'])
    item=post(source,source['platform']+':'+hashlib.sha256(url.encode()).hexdigest(),
              url,plain(row['text']),row['published_at'],row.get('media',[]))
    item['capture_mode']=row['capture_mode']
    if row.get('published_at') and not item['published_at']:
        try:
            stamp=dt.datetime.fromisoformat(str(row['published_at']))
            if stamp.tzinfo is None:
                item['published_at_raw']=str(row['published_at'])
                item['publication_timezone_unknown']=True
        except ValueError:pass
    item['text_may_be_truncated']=row['capture_mode']=='public_article_excerpt'
    for key in ('markdown','extraction_tools','extraction_warning','markdown_warning','outbound_social_links','outbound_profile_links'):
        if key in row:item[key]=row[key]
    return item
