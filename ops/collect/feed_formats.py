"""RSS/Atom and JSON Feed share the same normalized entry/canonical URL path."""
import json
import feedparser


def parse(body):
    marker=b'{' if isinstance(body,bytes) else '{'
    if not body.lstrip().startswith(marker):
        return feedparser.parse(body)
    try:value=json.loads(body)
    except (ValueError,UnicodeError):return feedparser.parse(body)
    if not isinstance(value,dict) or value.get('version') not in ('https://jsonfeed.org/version/1','https://jsonfeed.org/version/1.1'):
        return feedparser.parse(body)
    rows=value.get('items')
    if not isinstance(rows,list):raise ValueError('invalid_json_feed')
    entries=[]
    for row in rows:
        if not isinstance(row,dict):continue
        content=row.get('content_html') or row.get('content_text') or ''
        entry={'id':str(row.get('id','')),'link':row.get('url') or row.get('external_url') or '',
               'title':row.get('title',''),'published':row.get('date_published'),
               'updated':row.get('date_modified'),'summary':row.get('summary',''),
               'content':[{'value':content}] if content else [],
               'enclosures':[{'href':a['url']} for a in row.get('attachments',[]) if isinstance(a,dict) and isinstance(a.get('url'),str)],
               'media_thumbnail':[{'url':row['image']}] if isinstance(row.get('image'),str) else []}
        entries.append(entry)
    links=[{'rel':'next','href':value['next_url']}] if isinstance(value.get('next_url'),str) else []
    return feedparser.FeedParserDict(version='jsonfeed',entries=entries,feed=feedparser.FeedParserDict(links=links),bozo=False)
