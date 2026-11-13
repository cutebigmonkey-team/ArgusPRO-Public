"""Earliest confirmed publication at a source since its last successful live check."""
from datetime import timedelta
from source_health import instant
from depth import CONFIG

SIGNAL_WINDOW=timedelta(minutes=CONFIG['live']['news_signal_minutes'])


def pending_news(items,live,when):
    cutoff=when-SIGNAL_WINDOW
    signals={}
    for post in items:
        sid=post.get('source_id')
        if not sid or post.get('sample') or not post.get('published_at') or post.get('published_at_is_estimated'):
            continue
        stamp=instant(post['published_at'])
        if not cutoff<=stamp<=when:continue
        checked=live.get(sid,{})
        if checked.get('url')==post.get('source_url') and checked.get('last_success_at') and stamp<=instant(checked['last_success_at']):
            continue
        key=(stamp,post['id'])
        if sid not in signals or key<signals[sid][0]:
            signals[sid]=(key,{'url':post.get('source_url'),'post_id':post['id'],'published_at':stamp.isoformat()})
    return {sid:row for sid,(_,row) in signals.items()}


def telegram_head(source):
    """Read once; the history collector consumes the same verified public response."""
    from urllib.parse import urlsplit
    from run import fetch,telegram_html
    owner=urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    url='https://t.me/s/'+owner
    body,final=fetch(url,timeout=7)
    if urlsplit(final).hostname!='t.me' or urlsplit(final).path.rstrip('/')!='/s/'+owner:
        raise ValueError('unexpected_telegram_redirect')
    return (body,final),telegram_html(source,100,body)
