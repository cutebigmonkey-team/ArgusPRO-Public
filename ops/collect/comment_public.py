"""Read public discussion surfaces, with explicit post identity and bounded pages.

Telegram's documented discussion widget and Threads' SSR records need no user
keys or account cookies. No login, challenge solving, or proxy fallback occurs.
"""
import json
import re
import urllib.error
from urllib.parse import urlsplit,urljoin,urlencode,parse_qs
from bs4 import BeautifulSoup

THREAD_HOSTS={'threads.com','www.threads.com','threads.net','www.threads.net'}


def answer(rows=(),status='partial',reason='public_page_excerpt',cursor=None):
    return dict(rows=list(rows),status=status,reason=reason,cursor=cursor,retry_after=0)


def walk(value):
    stack=[value];count=0
    while stack and count<100000:
        item=stack.pop();count+=1
        if isinstance(item,dict):
            yield item
            stack.extend(reversed([v for v in item.values() if isinstance(v,(dict,list))]))
        elif isinstance(item,list):stack.extend(reversed(item))


def payloads(body):
    soup=BeautifulSoup(body,'html.parser')
    for script in soup.select('script[type="application/json"],script[type="application/ld+json"]'):
        try:yield json.loads(script.string or script.get_text())
        except (ValueError,RecursionError):continue


def fetch_page(url,hosts,*,crawler=False):
    from run import safe_url
    from http_pool import request
    from browser_public import page_error
    # Same crawler representation used by the supplied Threads research CLI.
    ua='Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)' if crawler else 'ArgusPRO/1.0 public-feed-reader'
    for _ in range(3):
        u=urlsplit(url)
        if u.scheme!='https' or u.hostname not in hosts or u.port or u.username or u.password:raise ValueError('invalid_comment_endpoint')
        safe_url(url)
        try:raw,final=request(url,{'User-Agent':ua,'Accept':'text/html','Accept-Language':'en-US,en;q=0.9'},7)
        except urllib.error.HTTPError as error:
            if error.code not in (301,302,303,307,308) or not error.headers.get('Location'):raise
            url=urljoin(url,error.headers['Location']);continue
        body=raw.decode('utf-8','replace')
        failure=page_error(final,body)
        if failure:return '',final,answer(status='auth_required' if failure=='login_required' else 'forbidden',reason=failure)
        return body,final,None
    raise ValueError('comment_redirect_limit')


def telegram_target(url):
    u=urlsplit(url)
    if u.hostname not in ('t.me','www.t.me','telegram.me') or u.scheme!='https':return None
    m=re.fullmatch(r'/(?:s/)?([A-Za-z0-9_]{3,64})/(\d+)/?',u.path)
    return 'https://t.me/'+m[1]+'/'+m[2] if m else None


def init_json(body,name):
    m=re.search(re.escape(name)+r'\.init\s*\(\s*',body)
    if not m:return {}
    try:
        value,_=json.JSONDecoder().raw_decode(body[m.end():])
        return value if isinstance(value,dict) else {}
    except ValueError:return {}


def telegram_html(body,target,*,fragment=False):
    soup=BeautifulSoup(body,'html.parser')
    if not fragment:
        header=soup.select_one('.tgme_post_discussion_header a[href]')
        if header and telegram_target(header['href'])!=target:
            return answer(status='failed',reason='discussion_post_mismatch')
    rows={}
    for node in soup.select('.tgme_widget_message[data-post-id]'):
        identity=node.get('data-post-id','')
        if not re.fullmatch(r'\d+',identity):continue
        text=node.select_one('.js-message_text')
        if not text:continue
        # Quoted reply previews must never become part of the comment body.
        for br in text.select('br'):br.replace_with('\n')
        content=text.get_text('',strip=False).strip()
        if not content:continue
        author=node.select_one('.tgme_widget_message_author_name')
        date=node.select_one('.tgme_widget_message_date time[datetime]')
        parent=None
        reply=node.select_one('.tgme_widget_message_reply')
        if reply:
            candidate=reply.get('data-post-id') or parse_qs(urlsplit(reply.get('href','')).query).get('comment',[''])[0]
            if re.fullmatch(r'\d+',candidate) and candidate!=identity:parent=candidate
        rows[identity]=dict(id=identity,text=content,author=author.get_text(' ',strip=True) if author else None,
            published_at=date.get('datetime') if date else None,parent_id=parent,url=target+'?comment='+identity)
        if len(rows)>=100:break
    more=soup.select_one('.js-messages_more[data-before]')
    before=more.get('data-before') if more else None
    if before and not re.fullmatch(r'\d+',before):before=None
    if rows:return answer(rows.values(),reason='telegram_public_discussion',cursor={'before':before} if before else None)
    empty=soup.select_one('.tme_no_messages_found,.tgme_widget_message_error')
    message=empty.get_text(' ',strip=True).lower() if empty else ''
    if any(v in message for v in ('open telegram','not available','violated','not found')):
        return answer(status='forbidden',reason='telegram_discussion_unavailable')
    state=init_json(body,'TWidgetDiscussion')
    if not fragment and type(state.get('comments_cnt')) is int and state['comments_cnt']==0:
        return answer(status='success',reason='no_comments')
    return answer(status='failed',reason='telegram_discussion_not_verified')


def telegram_public(post,cursor):
    target=telegram_target(post['url'])
    if not target:return answer(status='unsupported',reason='public_channel_post_required')
    body,final,error=fetch_page(target+'?embed=1&discussion=1&comments_limit=100',{'t.me','www.t.me'})
    if error:return error
    if telegram_target(final)!=target:return answer(status='failed',reason='discussion_post_mismatch')
    first=telegram_html(body,target)
    if not cursor or not cursor.get('before') or first['status'] not in ('partial','success'):return first
    before=str(cursor['before'])
    if not re.fullmatch(r'\d{1,20}',before):return answer(status='failed',reason='invalid_cursor')
    auth=init_json(body,'TWidgetAuth');endpoint=auth.get('api_url','')
    u=urlsplit(endpoint)
    if u.scheme!='https' or u.hostname!='t.me' or u.path.rstrip('/')!='/api' or u.port or u.username or u.password:
        return answer(first['rows'],reason='telegram_public_window_only')
    soup=BeautifulSoup(body,'html.parser');params={'method':'loadComments','before_id':before}
    for name in ('peer','top_msg_id','discussion_hash'):
        field=soup.select_one('form input[name="'+name+'"]')
        if not field or not field.get('value'):return answer(first['rows'],reason='telegram_public_window_only')
        params[name]=field['value'][:512]
    from run import safe_url
    from http_pool import POOL,MAX_BODY
    import urllib3
    safe_url(endpoint)
    response=POOL.request('POST',endpoint,body=urlencode(params).encode(),headers={
        'Content-Type':'application/x-www-form-urlencoded','X-Requested-With':'XMLHttpRequest',
        'User-Agent':'ArgusPRO/1.0 public-feed-reader','Referer':target+'?embed=1&discussion=1'},
        redirect=False,retries=False,timeout=urllib3.Timeout(connect=3,read=7,total=8),preload_content=False)
    try:
        if response.status>=300:raise urllib.error.HTTPError(endpoint,response.status,'discussion_http_error',response.headers,None)
        raw=response.read(MAX_BODY+1)
        if len(raw)>MAX_BODY:raise ValueError('response_too_large')
        data=json.loads(raw)
    finally:response.close();response.release_conn()
    if not isinstance(data,dict) or not isinstance(data.get('comments_html'),str):return answer(status='failed',reason='telegram_discussion_not_verified')
    page=telegram_html(data['comments_html'],target,fragment=True)
    if page.get('cursor') and int(page['cursor']['before'])>=int(before):page['cursor']=None
    return page


def threads_code(url):
    u=urlsplit(url)
    if u.scheme!='https' or u.hostname not in THREAD_HOSTS:return None
    match=re.fullmatch(r'/@([\w.]{1,64})/post/([\w-]{1,64})(?:/media)?/?',u.path,re.ASCII)
    return match if match else None


def threads_payloads(post,documents):
    match=threads_code(post['url'])
    if not match:return answer(status='unsupported',reason='invalid_threads_url')
    code=match[2];root_id=0
    for ch in code:root_id=root_id*64+'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'.index(ch)
    root_id=str(root_id)
    nodes=[n for doc in documents for n in walk(doc)]
    def identity(node):return str(node.get('pk') or node.get('id') or '').split('_')[0]
    roots=[n for n in nodes if n.get('code')==code or identity(n)==root_id]
    # Cross-check shortcode and numeric identity, then only descend into replies.
    roots=[n for n in roots if not n.get('code') or n['code']==code]
    if not roots:return answer(status='failed',reason='threads_post_not_verified')
    rows={};verified=False;more=False;unavailable=False;reported=[]
    def add(node,parent=None):
        ident=identity(node);short=node.get('code');user=node.get('user') or {};owner=user.get('username')
        text=(node.get('caption') or {}).get('text')
        if ident==root_id or not re.fullmatch(r'\d+',ident) or not isinstance(text,str) or not text.strip():return
        if not isinstance(short,str) or not re.fullmatch(r'[\w-]+',short,re.ASCII) or not isinstance(owner,str) or not re.fullmatch(r'[\w.]+',owner,re.ASCII):return
        rows[ident]=dict(id=ident,parent_id=parent if parent and parent!=root_id else None,text=text,author=owner,
            published_at=node.get('taken_at'),likes=node.get('like_count'),url='https://www.threads.com/@'+owner+'/post/'+short)
    def connection(value,parent,depth=0):
        nonlocal more
        if depth>12 or len(rows)>=200 or not isinstance(value,dict):return
        if (value.get('page_info') or {}).get('has_next_page'):more=True
        for edge in value.get('edges') or []:
            node=edge.get('node',{}) if isinstance(edge,dict) else {}
            candidate=node.get('post',node)
            if isinstance(candidate,dict):
                add(candidate,parent)
                info=candidate.get('text_post_app_info') or {}
                connection(info.get('direct_replies'),identity(candidate),depth+1)
                # Newer SSR connections wrap each reply chain in posts.edges.
                connection(node.get('posts'),parent,depth+1)
                if isinstance(node.get('thread'),dict):connection(node['thread'].get('posts'),parent,depth+1)
            if len(rows)>=200:more=True;break
    for root in roots:
        info=root.get('text_post_app_info') or {}
        if type(info.get('direct_reply_count')) is int:reported.append(info['direct_reply_count'])
        unavailable|=bool(info.get('has_unavailable_replies'))
        for field in ('direct_replies','pinned_replies'):
            if isinstance(info.get(field),dict) and isinstance(info[field].get('edges'),list):
                verified=True;connection(info[field],root_id)
    # Legacy chain shape is accepted only if the target root is in that chain.
    for node in nodes:
        items=node.get('thread_items')
        if not isinstance(items,list):continue
        chain=[i.get('post',{}) for i in items if isinstance(i,dict)]
        start=next((i for i,p in enumerate(chain) if p.get('code')==code or identity(p)==root_id),None)
        if start is None:continue
        parent=root_id
        for p in chain[start+1:]:
            if (p.get('text_post_app_info') or {}).get('reply_to_author'):
                add(p,parent);parent=identity(p)
    if rows:return answer(list(rows.values())[:200],reason='threads_public_replies')
    if verified and reported and max(reported)==0 and not more and not unavailable:return answer(status='success',reason='no_comments')
    return answer(status='partial' if verified else 'failed',reason='threads_replies_not_exposed')


def threads_public(post,cursor):
    match=threads_code(post['url'])
    if not match:return answer(status='unsupported',reason='invalid_threads_url')
    url='https://www.threads.com/@'+match[1]+'/post/'+match[2]
    body,final,error=fetch_page(url,THREAD_HOSTS,crawler=True)
    if error:return error
    target=threads_code(final)
    if not target or target[2]!=match[2]:return answer(status='failed',reason='discussion_post_mismatch')
    return threads_payloads(post,payloads(body))


def structured_comments(post,documents):
    """Schema.org comments must belong to the exact requested publication."""
    def same(value):
        if isinstance(value,dict):value=value.get('@id') or value.get('url')
        if not isinstance(value,str):return False
        u=urlsplit(value);v=urlsplit(post['url'])
        return u.scheme=='https' and u.hostname==v.hostname and u.path.rstrip('/')==v.path.rstrip('/') and u.query==v.query
    rows={}
    def add(node,parent=None):
        if not isinstance(node,dict) or len(rows)>=200:return
        kind=node.get('@type',[]);kind=[kind] if isinstance(kind,str) else kind
        if 'Comment' not in kind:return
        identity=node.get('@id') or node.get('identifier') or node.get('url')
        content=node.get('text') or node.get('description')
        if not isinstance(identity,(str,int)) or not isinstance(content,str):return
        author=node.get('author',{});name=author.get('name') if isinstance(author,dict) else author if isinstance(author,str) else None
        rows[str(identity)]=dict(id=str(identity),parent_id=parent,text=BeautifulSoup(content,'html.parser').get_text(' ',strip=True),
            author=name,published_at=node.get('datePublished'),url=node.get('url') or (identity if isinstance(identity,str) and identity.startswith('https://') else ''))
        children=node.get('comment') or []
        for child in children if isinstance(children,list) else [children]:add(child,str(identity))
    for doc in documents:
        for node in walk(doc):
            if same(node.get('url')) or same(node.get('@id')) or same(node.get('mainEntityOfPage')):
                children=node.get('comment') or []
                for child in children if isinstance(children,list) else [children]:add(child)
            elif same(node.get('parentItem')) or same(node.get('about')):add(node)
    return list(rows.values())


def x_payload(post,documents):
    match=re.search(r'/status/(\d+)',urlsplit(post['url']).path)
    if not match:return []
    root=match[1];rows={}
    for doc in documents:
        for node in walk(doc):
            legacy=node.get('legacy')
            if not isinstance(legacy,dict):continue
            parent=str(legacy.get('in_reply_to_status_id_str') or '')
            if not parent or str(legacy.get('conversation_id_str') or '')!=root:continue
            identity=str(node.get('rest_id') or legacy.get('id_str') or '')
            if not re.fullmatch(r'\d+',identity) or identity==root or not isinstance(legacy.get('full_text'),str):continue
            user=(((node.get('core') or {}).get('user_results') or {}).get('result') or {})
            author=(user.get('legacy') or {}).get('screen_name') or (user.get('core') or {}).get('screen_name')
            if not isinstance(author,str) or not re.fullmatch(r'\w{1,30}',author,re.ASCII):continue
            from email.utils import parsedate_to_datetime
            try:stamp=parsedate_to_datetime(legacy.get('created_at','')).isoformat()
            except (TypeError,ValueError):stamp=None
            rows[identity]=dict(id=identity,parent_id=parent if parent!=root else None,text=legacy['full_text'],
                author=author,published_at=stamp,likes=legacy.get('favorite_count'),url='https://x.com/'+author+'/status/'+identity)
            if len(rows)>=200:return list(rows.values())
    return list(rows.values())


def vk_html(post,body):
    match=re.fullmatch(r'/wall(-?\d+_\d+)',urlsplit(post['url']).path)
    if not match:return []
    target=match[1];soup=BeautifulSoup(body,'html.parser');root=soup.find(id='post'+target)
    if root is None:return []
    rows={}
    for node in root.select('.reply[id],.reply[data-reply-id]'):
        identity=node.get('data-reply-id') or node.get('id','').removeprefix('post')
        text=node.select_one('.wall_reply_text,.reply_text');author=node.select_one('.author,.reply_author')
        if not re.fullmatch(r'-?\d+_\d+|\d+',identity) or not text:continue
        # The discussion's own root container is mandatory; neighbouring walls are excluded.
        rid=identity.rsplit('_',1)[-1]
        canonical=target.split('_')[0]+'_'+rid
        rows[canonical]=dict(id=canonical,text=text.get_text(' ',strip=True),author=author.get_text(' ',strip=True) if author else None,
            url=post['url']+'?reply='+rid,published_at=None)
        if len(rows)>=100:break
    return list(rows.values())


def remaining_public(post,cursor):
    from run import HOSTS
    platform=post['platform'];hosts=set(HOSTS.get(platform,()))
    if not hosts or urlsplit(post['url']).hostname not in hosts:return answer(status='unsupported',reason='invalid_social_url')
    body,final,error=fetch_page(post['url'],hosts)
    if error:return error
    # No redirected profile or login shell may be associated with the original post.
    if urlsplit(final).path.rstrip('/')!=urlsplit(post['url']).path.rstrip('/'):
        return answer(status='failed',reason='discussion_post_mismatch')
    documents=list(payloads(body));rows=structured_comments(post,documents)
    if platform=='x':rows+=x_payload(post,documents)
    if platform=='vk':rows+=vk_html(post,body)
    return answer(rows,reason='public_page_excerpt') if rows else answer(status='partial',reason='public_comments_not_exposed')
