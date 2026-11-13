"""Read owned Threads posts from the public page's embedded JSON, without a session."""
import re
import json
from html.parser import HTMLParser
from urllib.parse import urlsplit
from comment_public import walk
from cloud_social import threads_target
from public_embeds import timestamp


def page_data(body):
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.hidden=False;self.json_script=False;self.documents=[];self.visible=[];self.script=[]
        def handle_starttag(self,tag,attrs):
            if tag in ('script','style'):
                self.hidden=True
                self.json_script=tag=='script' and dict(attrs).get('type') in ('application/json','application/ld+json')
                self.script=[]
        def handle_data(self,data):
            if self.json_script:self.script.append(data)
            elif not self.hidden and len(self.visible)<1000:self.visible.append(data[:1000])
        def handle_endtag(self,tag):
            if tag in ('script','style'):
                if self.json_script:
                    try:self.documents.append(json.loads(''.join(self.script)))
                    except (ValueError,RecursionError):pass
                self.hidden=False;self.json_script=False;self.script=[]
    reader=Reader();reader.feed(body.decode('utf-8',errors='replace'))
    return reader.documents,' '.join(reader.visible)[:65536]


def parse(source, body, limit, documents=None):
    from run import post
    owner, requested = threads_target(source)
    items = {}
    for document in page_data(body)[0] if documents is None else documents:
        for node in walk(document):
            user = node.get('user')
            code = node.get('code')
            if not isinstance(user, dict) or str(user.get('username', '')).lower() != owner.lower():continue
            if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', code):continue
            if requested and code != requested:continue
            # Recommendations, replies and repost wrappers are not this publisher's timeline posts.
            info = node.get('text_post_app_info') or {}
            if not isinstance(info, dict) or info.get('reply_to_author') or info.get('reply_to_id'):continue
            caption = node.get('caption')
            text = caption.get('text', '') if isinstance(caption, dict) else ''
            media = []
            for content in [node] + (node.get('carousel_media') or []):
                if not isinstance(content, dict):continue
                images = content.get('image_versions2') or {}
                if isinstance(images, dict):
                    media.extend(r.get('url') for r in (images.get('candidates') or []) if isinstance(r, dict))
                media.extend(r.get('url') for r in (content.get('video_versions') or []) if isinstance(r, dict))
            if not isinstance(text, str) or not text.strip() and not media:continue
            item = post(source, 'threads:'+code, 'https://www.threads.com/@'+owner+'/post/'+code,
                        text, timestamp(node.get('taken_at')), media)
            item.update(capture_mode='threads_public_json', author=owner, text_may_be_truncated=True)
            item['engagement'] = {k:node[k] for k in ('like_count','reply_count','repost_count','quote_count')
                                  if type(node.get(k)) is int and node[k]>=0}
            items[item['canonical_key']] = item
            if len(items)>=limit:return list(items.values())
    return list(items.values())


def collect(source, limit):
    from run import fetch
    from browser_public import page_error
    owner, requested = threads_target(source)
    url = 'https://www.threads.com/@'+owner+('/post/'+requested if requested else '')
    body, final = fetch(url, timeout=8)
    if urlsplit(final).hostname not in ('www.threads.com','threads.com','www.threads.net','threads.net'):
        raise ValueError('unexpected_destination')
    documents, visible = page_data(body)
    failure = page_error(final, visible)
    if failure:raise ValueError(failure)
    if threads_target({**source,'url':final}) != (owner, requested):raise ValueError('unexpected_destination')
    rows = parse(source, body, limit, documents)
    return rows, {'tool':'threads_public_json','status':'ok' if rows else 'unverified_markup','posts':len(rows)}
