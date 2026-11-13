"""Optional read-only official APIs. No provisioning, login or paid fallback.

Only credentials already supplied by the operator are used. Persist tokens from
pagination, never server-provided URLs (which can contain access credentials).
"""
import json
import os
import re
import urllib.error
from urllib.parse import urlencode, urlsplit


ENVIRONMENT = {
    'ARGUS_X_BEARER_TOKEN', 'ARGUS_FACEBOOK_COMMENTS_TOKEN',
    'ARGUS_INSTAGRAM_COMMENTS_TOKEN', 'ARGUS_THREADS_COMMENTS_TOKEN',
    'ARGUS_META_API_VERSION', 'ARGUS_VK_COMMENTS_TOKEN',
    'ARGUS_TIKTOK_RESEARCH_TOKEN',
}


def api(url, token, body=None):
    from comment_adapters import get
    if body is None:
        return get(url, {'Authorization': 'Bearer '+token})
    from http_pool import POOL, MAX_BODY
    import urllib3
    # POST is used only for TikTok's documented read-only research query.
    if urlsplit(url).hostname != 'open.tiktokapis.com':
        raise ValueError('invalid_comment_endpoint')
    response = POOL.request('POST', url, body=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'},
        redirect=False, retries=False, preload_content=False, pool_timeout=8,
        timeout=urllib3.Timeout(connect=3, read=7, total=8))
    try:
        if response.status >= 300:
            raise urllib.error.HTTPError(url, response.status, 'comment_api_error', response.headers, None)
        raw = response.read(MAX_BODY+1, decode_content=True)
        if len(raw) > MAX_BODY:
            raise ValueError('response_too_large')
        return json.loads(raw)
    finally:
        response.close()
        response.release_conn()


def error_result(data):
    from comment_adapters import result
    error = data.get('error') or {}
    code = error.get('code', error.get('error_code'))
    if code in (None, 'ok'):
        return None
    if code in (4, 6, 17, 32, 613, 'rate_limit_exceeded'):
        status = 'rate_limited'
    elif code in (5, 102, 190, 'access_token_invalid', 'access_token_expired'):
        status = 'auth_required'
    else:
        status = 'forbidden'
    # Never expose upstream messages, request URLs, or tokens in health reports.
    return result(status=status, reason='api_'+str(code)[:50])


def graph(post, cursor, platform):
    from comment_adapters import result
    token = os.environ['ARGUS_'+platform.upper()+'_COMMENTS_TOKEN']
    identity = str(post.get('platform_post_id') or '')
    if not re.fullmatch(r'\d+(?:_\d+)?', identity):
        return result(status='unsupported', reason='verified_platform_post_id_required')
    version = os.getenv('ARGUS_META_API_VERSION', '')
    if platform != 'threads' and not re.fullmatch(r'v\d+\.\d+', version):
        return result(status='unsupported', reason='meta_api_version_required')
    host = {'facebook':'graph.facebook.com', 'instagram':'graph.instagram.com', 'threads':'graph.threads.net'}[platform]
    state = dict(cursor or {})
    replies = list(state.get('replies', []))
    parent = replies[0] if replies else None
    if parent and not re.fullmatch(r'\d+', parent):
        raise ValueError('invalid_cursor')
    target = parent or identity
    edge = 'replies' if parent else 'conversation' if platform == 'threads' else 'comments'
    fields = {
        'facebook':'id,message,from,created_time,like_count,parent,permalink_url',
        'instagram':'id,text,username,timestamp,like_count',
        'threads':'id,text,username,timestamp,permalink,replied_to',
    }[platform]
    params = {'fields':fields, 'limit':100}
    after = state.get('reply_page' if parent else 'page')
    if after:
        params['after'] = after
    if platform == 'facebook':
        params.update(filter='stream', order='chronological')
    prefix = version+'/' if platform != 'threads' else 'v1.0/'
    data = api('https://'+host+'/'+prefix+target+'/'+edge+'?'+urlencode(params), token)
    failure = error_result(data)
    if failure:
        return failure
    nodes = data.get('data')
    if not isinstance(nodes, list):
        raise ValueError('invalid_response')
    paging = data.get('paging') or {}
    next_page = (paging.get('cursors') or {}).get('after') if paging.get('next') else None
    if paging.get('next') and not next_page:
        raise ValueError('missing_page_cursor')
    if parent:
        if next_page:
            state['reply_page'] = next_page
        else:
            replies.pop(0)
            state.pop('reply_page', None)
        state['replies'] = replies
    else:
        state['page'] = next_page
        # Query every root's replies: a preview is not a complete reply listing.
        state['replies'] = [str(n['id']) for n in nodes] if platform == 'instagram' else []
    rows = []
    for n in nodes:
        reply_to = (n.get('parent') or n.get('replied_to') or {}).get('id') or parent
        rows.append(dict(id=n['id'], text=n.get('message', n.get('text', '')),
            parent_id=reply_to if reply_to != identity else None,
            author=(n.get('from') or {}).get('name') or n.get('username'),
            published_at=n.get('created_time') or n.get('timestamp'),
            likes=n.get('like_count'), url=n.get('permalink_url') or n.get('permalink')))
    more = bool(state.get('page') or state.get('replies'))
    return result(rows, 'partial' if more else 'success', 'authorized_api', state if more else None)


def facebook(post, cursor):
    if os.getenv('ARGUS_FACEBOOK_COMMENTS_TOKEN'):
        return graph(post, cursor, 'facebook')
    from comment_adapters import social_public
    return social_public(post, cursor)


def instagram(post, cursor):
    if os.getenv('ARGUS_INSTAGRAM_COMMENTS_TOKEN'):
        return graph(post, cursor, 'instagram')
    from comment_adapters import social_public
    return social_public(post, cursor)


def threads(post, cursor):
    if os.getenv('ARGUS_THREADS_COMMENTS_TOKEN'):
        return graph(post, cursor, 'threads')
    from comment_public import threads_public
    return threads_public(post, cursor)


def x(post, cursor):
    from comment_adapters import result
    token = os.getenv('ARGUS_X_BEARER_TOKEN')
    if not token:
        from comment_public import remaining_public
        return remaining_public(post, cursor)
    match = re.search(r'/status/(\d+)', urlsplit(post['url']).path)
    if not match:
        return result(status='unsupported', reason='post_id_missing')
    root = str(post.get('conversation_id') or match[1])
    if not re.fullmatch(r'\d+', root):
        raise ValueError('invalid_conversation_id')
    # A reply's conversation_id includes siblings. Do not associate them with
    # the reply as if it were a root publication.
    if root != match[1]:
        return result(status='unsupported', reason='conversation_root_required')
    params = {'query':'conversation_id:'+root+' is:reply', 'max_results':100,
        'post.fields':'created_at,author_id,conversation_id,referenced_posts,public_metrics,note_post',
        'expansions':'author_id', 'user.fields':'username'}
    if cursor:
        params['next_token'] = cursor['page']
    data = api('https://api.x.com/2/tweets/search/recent?'+urlencode(params), token)
    if not isinstance(data.get('meta'), dict) or not isinstance(data.get('data', []), list):
        raise ValueError('invalid_response')
    authors = {u['id']:u.get('username') for u in data.get('includes', {}).get('users', [])}
    rows = []
    for n in data.get('data', []):
        parent = next((r['id'] for r in n.get('referenced_posts', n.get('referenced_tweets', [])) if r['type']=='replied_to'), None)
        if n.get('conversation_id') != root or not parent:
            continue
        rows.append(dict(id=n['id'], text=(n.get('note_post') or n.get('note_tweet') or {}).get('text') or n.get('text', ''), parent_id=parent if parent!=root else None,
            author=authors.get(n.get('author_id')), published_at=n.get('created_at'),
            likes=(n.get('public_metrics') or {}).get('like_count'), url='https://x.com/i/web/status/'+n['id']))
    page = data['meta'].get('next_token')
    if data.get('errors'):
        return result(rows, 'failed', 'x_search_incomplete')
    # Recent search is a time window, never proof that the entire thread is read.
    return result(rows, 'partial', 'x_recent_search_window', {'page':page} if page else None)


def tiktok(post, cursor):
    from comment_adapters import result
    token = os.getenv('ARGUS_TIKTOK_RESEARCH_TOKEN')
    if not token:
        from comment_adapters import social_public
        return social_public(post, cursor)
    match = re.search(r'/video/(\d+)', urlsplit(post['url']).path)
    if not match:
        return result(status='unsupported', reason='video_id_missing')
    state = dict(cursor or {})
    replies = list(state.get('replies', []))
    parent = replies[0] if replies else None
    body = {'comment_id' if parent else 'video_id':int(parent or match[1]),
        'max_count':100, 'cursor':state.get('reply_page' if parent else 'page', 0)}
    fields = 'id,video_id,text,like_count,reply_count,parent_comment_id,create_time'
    data = api('https://open.tiktokapis.com/v2/research/video/comment/list/?fields='+fields, token, body)
    failure = error_result(data)
    if failure:
        return failure
    data = data.get('data') or {}
    if not isinstance(data.get('comments'), list) or type(data.get('has_more')) is not bool:
        raise ValueError('invalid_response')
    page = data.get('cursor') if data['has_more'] else None
    if data['has_more'] and (type(page) is not int or page <= body['cursor']):
        raise ValueError('pagination_stalled')
    nodes = data['comments']
    if parent:
        if page is not None:
            state['reply_page'] = page
        else:
            replies.pop(0)
            state.pop('reply_page', None)
        state['replies'] = replies
    else:
        state = {'page':page, 'replies':[str(n['id']) for n in nodes if n.get('reply_count', 0)>0]}
    rows = []
    for n in nodes:
        if n.get('video_id') and str(n['video_id']) != match[1]:
            raise ValueError('discussion_post_mismatch')
        rows.append(dict(id=str(n['id']), text=n.get('text', ''), author=None,
            parent_id=str(n.get('parent_comment_id') or parent or '') or None,
            published_at=n.get('create_time'), likes=n.get('like_count')))
    more = state.get('page') is not None or bool(state.get('replies'))
    return result(rows, 'partial' if more else 'success', 'tiktok_research_snapshot', state if more else None)


def vk(post, cursor):
    from comment_adapters import result, get
    token = os.getenv('ARGUS_VK_COMMENTS_TOKEN')
    if not token:
        from comment_public import remaining_public
        return remaining_public(post, cursor)
    match = re.fullmatch(r'/wall(-?\d+)_(\d+)/?', urlsplit(post['url']).path)
    if not match:
        return result(status='unsupported', reason='wall_post_required')
    state = dict(cursor or {})
    replies = list(state.get('replies', []))
    parent = replies[0] if replies else None
    offset = int(state.get('reply_offset' if parent else 'offset', 0))
    params = dict(owner_id=match[1], post_id=match[2], count=100, offset=offset,
        sort='asc', need_likes=1, extended=1, thread_items_count=0, v='5.199', access_token=token)
    if parent:
        params['comment_id'] = parent
    data = get('https://api.vk.com/method/wall.getComments?'+urlencode(params))
    failure = error_result(data)
    if failure:
        return failure
    data = data.get('response') or {}
    nodes = data.get('items')
    if not isinstance(nodes, list) or type(data.get('count')) is not int:
        raise ValueError('invalid_response')
    # count can include nested replies; pagination must use the current level.
    total = data.get('current_level_count', data['count'])
    more_page = offset+len(nodes)<total
    if more_page and not nodes:
        raise ValueError('pagination_stalled')
    if parent:
        if more_page:
            state['reply_offset'] = offset+len(nodes)
        else:
            replies.pop(0)
            state.pop('reply_offset', None)
        state['replies'] = replies
    else:
        state = {'offset':offset+len(nodes), 'more':more_page,
            'replies':[str(n['id']) for n in nodes if (n.get('thread') or {}).get('count', 0)>0]}
    authors = {n['id']:' '.join(filter(None, [n.get('first_name'), n.get('last_name')])) for n in data.get('profiles', [])}
    authors.update({-n['id']:n.get('name') for n in data.get('groups', [])})
    rows = [dict(id=match[1]+'_'+str(n['id']), text=n.get('text', ''), author=authors.get(n.get('from_id')),
        parent_id=(match[1]+'_'+str(n.get('reply_to_comment') or parent)) if n.get('reply_to_comment') or parent else None, published_at=n.get('date'),
        likes=(n.get('likes') or {}).get('count'), url=post['url']+'?reply='+str(n['id'])) for n in nodes]
    more = state.get('more') or bool(state.get('replies'))
    return result(rows, 'partial' if more else 'success', 'authorized_api', state if more else None)


def ok(post, cursor):
    from comment_public import remaining_public
    return remaining_public(post, cursor)
