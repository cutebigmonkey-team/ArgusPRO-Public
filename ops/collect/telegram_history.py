"""Resume public Telegram history only after the batch is archived."""
import re
import time
from urllib.parse import urlsplit,parse_qs,urljoin
from bs4 import BeautifulSoup


def next_before(source,body,before=None):
    channel=urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    soup=BeautifulSoup(body,'html.parser'); ids=[]
    for node in soup.select('.tgme_widget_message[data-post]'):
        match=re.fullmatch(re.escape(channel)+r'/(\d+)',node.get('data-post',''),re.I)
        if match and (before is None or int(match[1])<before): ids.append(int(match[1]))
    if not ids: return None,'no_verified_page'
    for anchor in soup.select('a.tme_messages_more[href],a[data-before]'):
        url=urlsplit(urljoin('https://t.me/s/'+channel,anchor.get('href','')))
        raw=parse_qs(url.query).get('before',[anchor.get('data-before','')])[0]
        if url.hostname!='t.me' or url.path.rstrip('/')!='/s/'+channel: continue
        # A pinned card can predate every post on this page. The official
        # backwards link, not the smallest card ID, defines the next page.
        if raw.isdigit() and 0<int(raw)<=max(ids) and (before is None or int(raw)<before):
            return int(raw),'more_public_history'
    return None,'public_history_exhausted'


def collect(source,previous=None,max_pages=3,fetcher=None,parser=None,pause=time.sleep,*,max_posts=10000,seconds=85,live=False):
    from cloud_social import require_cloud,telegram_page
    from run import fetch,error_code,now,telegram_html
    require_cloud()
    if not 1<=max_pages<=200: raise ValueError('invalid_page_budget')
    deadline=time.monotonic()+seconds
    channel=urlsplit(source['url']).path.strip('/').removeprefix('s/').split('/')[0]
    if not re.fullmatch(r'[A-Za-z0-9_]{3,64}',channel): raise ValueError('invalid_channel')
    previous=previous or {}; fetcher=fetcher or fetch; parser=parser or telegram_page
    result={}; pages=[]; before=previous.get('before'); update=None; exhausted=bool(previous.get('exhausted'))
    if before is not None and (type(before) is not int or before<1): raise ValueError('invalid_history_cursor')
    head=previous.get('head_id')
    gaps=[list(pair) for pair in previous.get('gaps',[])]
    if ((head is not None and (type(head) is not int or head<1)) or len(gaps)>16 or
            any(len(pair)!=2 or any(type(n) is not int for n in pair) or not 0<=pair[1]<pair[0] for pair in gaps)):
        raise ValueError('invalid_history_cursor')
    stop='page_budget'
    def queue_gap(high,low):
        if any(a>=high and b<=low for a,b in gaps):
            return
        gaps.append([high,low])
        if len(gaps)>16:
            gaps[-2:]=[[max(pair[0] for pair in gaps[-2:]),min(pair[1] for pair in gaps[-2:])]]

    def checkpoint():
        return {'url':source['url'],'before':before,'exhausted':exhausted,'updated_at':now(),
                'head_id':head,'gaps':[pair.copy() for pair in gaps],
                'scope':'anonymous_public_history','tool':'tg-radar+TelegramChannels'}
    for index in range(max_pages):
        if index and (len(result)>=max_posts or time.monotonic()>=deadline):
            stop='post_budget' if len(result)>=max_posts else 'time_budget'
            break
        if index and live and not gaps and head and previous.get('head_id'):
            stop='head_checkpoint'
            break
        fill_gap=index>0 and bool(gaps) and (live or index%2==1 or exhausted)
        cursor=None if index==0 else gaps[0][0] if fill_gap else before
        url='https://t.me/s/'+channel+('?'+'before='+str(cursor) if cursor else '')
        try:
            try:
                body,final=fetcher(url)
            except Exception as exc:
                if error_code(exc) not in ('timeout','network_error','http_502','http_503','http_504') or time.monotonic()+3>=deadline:raise
                import random
                pause(1+random.random())
                body,final=fetcher(url)
            if urlsplit(final).hostname!='t.me' or urlsplit(final).path.rstrip('/')!='/s/'+channel:
                raise ValueError('unexpected_telegram_redirect')
            parser_error = None
            cards=BeautifulSoup(body,'html.parser').select('.tgme_widget_message[data-post]')
            page_limit=max(1,len(cards))
            try:
                rows=parser(source,body,page_limit)
                # A parser can succeed while omitting media-only cards from the same page.
                owned={p['canonical_key']:p for p in rows}
                for row in telegram_html(source,page_limit,body):
                    owned.setdefault(row['canonical_key'],row)
                rows=list(owned.values())
            except Exception as error:
                parser_error = error_code(error)
                rows = telegram_html(source,page_limit,body)
                if not rows:
                    raise
            if cursor is not None: rows=[p for p in rows if int(p['canonical_key'].rsplit('/',1)[1])<cursor]
            for post in rows: result[post['canonical_key']]=post
            next_cursor,status=next_before(source,body,cursor)
            if not rows and status != 'no_verified_page':
                status = 'unverified_markup'
            pages.append({'before':cursor,'status':status,'posts':len(rows)})
            page = pages[-1]
            page['raw_rows'] = len(cards)
            page['candidates'] = []
            accepted = {row['canonical_key'] for row in rows}
            seen = set()
            for card in cards:
                identity = card.get('data-post', '')
                key = 'telegram:' + identity.lower()
                stamp = card.select_one('time[datetime]')
                reason = None
                if not re.fullmatch(re.escape(channel) + r'/\d+', identity, re.I):
                    reason = 'foreign_or_invalid_identity'
                elif key in seen:
                    reason = 'duplicate_card'
                elif cursor and int(identity.rsplit('/', 1)[1]) >= cursor:
                    reason = 'outside_cursor'
                elif key not in accepted:
                    reason = ('preview_unavailable' if card.select_one('.tgme_widget_message_error,.message_media_not_supported')
                              or 'view in telegram' in card.get_text(' ', strip=True).lower()
                              else 'no_public_text_or_media')
                seen.add(key)
                page['candidates'].append({'key':key, 'published_at':stamp.get('datetime') if stamp else None,
                                           'reason':reason})
            visible = {'telegram:'+n['data-post'].lower() for n in cards
                       if re.fullmatch(re.escape(channel)+r'/[1-9]\d*',n['data-post'],re.I) and
                       (cursor is None or int(n['data-post'].split('/')[1])<cursor)}
            missing = visible - {p['canonical_key'] for p in rows}
            if missing:
                pages[-1]['missing_owned_cards']=len(missing)
                if rows and not parser_error:
                    pages[-1]['status']='partial_telegram_cards'
                    if index==0:
                        ids=[int(key.rsplit('/',1)[1]) for key in visible]
                        # The head can move before the retry. Persist the entire unverified interval.
                        queue_gap(max(ids)+1,min(min(ids)-1,head) if head else min(ids)-1)
                        head=max(head or 0,max(ids))
                        if not previous:
                            before=next_cursor
                        update=checkpoint()
                        if index+1<max_pages:pause(1)
                        continue
                    break
            if parser_error:
                # Keep recoverable cards but retry this page before advancing the cursor.
                pages[-1].update(status=parser_error, fallback='telegram_html')
                break
            if status in ('no_verified_page','unverified_markup'): break
            if index==0:
                ids=[int(p['canonical_key'].rsplit('/',1)[1]) for p in rows]
                if not ids: break
                if head and next_cursor and next_cursor>head and max(ids)>head:
                    queue_gap(next_cursor,head)
                head=max([head or 0,*ids])
            if fill_gap:
                if not next_cursor or next_cursor<=gaps[0][1]:
                    gaps.pop(0)
                else:
                    gaps[0][0]=next_cursor
            elif index>0 or not previous:
                before=next_cursor; exhausted=status=='public_history_exhausted'
            update=checkpoint()
            if exhausted and not gaps:
                stop='public_history_exhausted'
                break
            pause(1)
        except Exception as error:
            status=error_code(error); pages.append({'before':cursor,'status':status,'posts':0}); break
    status=pages[-1]['status'] if pages else 'no_public_posts'
    overall='ok' if result and status in ('more_public_history','public_history_exhausted') else status
    if overall=='ok' and gaps and any(p['status']=='partial_telegram_cards' for p in pages):
        overall='partial_telegram_cards'
    return list(result.values()),overall,[{'tool':'telegram_public_history','status':overall,'posts':len(result),
        'raw_rows':sum(p.get('raw_rows',0) for p in pages),
        'rejected_rows':{'unparsed_owned_card':sum(p.get('missing_owned_cards',0) for p in pages)},
        'pages':pages,'stop_reason':stop if overall=='ok' else overall,'history_state':'public_history_exhausted' if exhausted and not gaps else 'pending',
        'full_platform_history_guaranteed':False}],update
