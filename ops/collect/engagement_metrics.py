"""Publisher counters only. Absent counters remain unknown, including zero."""
import re
from decimal import Decimal

ALIASES = {
    'views': ('views', 'view_count', 'viewCount', 'playCount', 'play_count', 'impression_count'),
    'likes': ('likes', 'like_count', 'likeCount', 'diggCount', 'digg_count', 'reactions', 'favorite_count'),
    'comments': ('comments', 'comments_count', 'comment_count', 'commentCount', 'reply_count', 'replyCount'),
    'shares': ('shares', 'shares_count', 'share_count', 'shareCount', 'reposts', 'repost_count', 'retweet_count', 'forwards'),
}

def counters(item):
    nested = item.get('engagement') if isinstance(item.get('engagement'), dict) else {}
    return {name: next((obj[key] for obj in (item, nested) for key in aliases
                       if type(obj.get(key)) is int and 0 <= obj[key] <= 9007199254740991), None)
            for name, aliases in ALIASES.items()}


def retain_counters(item, row):
    """Call only after the adapter has verified this exact post and owner."""
    values = counters(row)
    for field in ('stats', 'statistics', 'statsV2', 'public_metrics'):
        nested = row.get(field)
        if not isinstance(nested, dict): continue
        # TikTok statsV2 serializes integer counters as decimal strings.
        if field == 'statsV2':
            nested = {k:int(v) if isinstance(v,str) and re.fullmatch(r'\d{1,16}',v) else v
                      for k,v in nested.items()}
        for name, value in counters(nested).items():
            if value is not None: values[name] = value
    known = {k:v for k,v in values.items() if v is not None}
    if known:
        item.setdefault('engagement', {}).update(known)
        for name,value in known.items():
            field='comments_count' if name=='comments' else name
            if field in item:item[field]=value
    return item

def displayed_count(text):
    text = re.sub(r'[\s\u200b]', '', text).upper()
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+', text): text = text.replace(',', '')
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)([KMB]?)', text)
    if not match or (not match[2] and ('.' in match[1] or ',' in match[1])): return None
    value = Decimal(match[1].replace(',', '.')) * {'':1, 'K':1000, 'M':1000000, 'B':1000000000}[match[2]]
    return int(value) if value == int(value) and value <= 9007199254740991 else None

def telegram_counters(node):
    result = {}
    views = node.select_one('.tgme_widget_message_views')
    if views:
        value = displayed_count(views.get_text('', strip=True))
        if value is not None: result['views'] = value
    replies = node.select_one('.tgme_widget_message_replies_count, .tgme_widget_message_replies_text, .tgme_widget_message_replies')
    if replies:
        label = replies.get_text(' ', strip=True)
        match = re.fullmatch(r'(?:View\s+)?([\d.,\s]+[KMB]?)\s*(?:comments?|replies)', label, re.I)
        value = displayed_count(match[1] if match else label)
        if value is not None: result['comments'] = value
    reactions = node.select('.tgme_widget_message_reaction, .tgme_reaction')
    values = []
    for reaction in reactions:
        count = reaction.select_one('.tgme_widget_message_reaction_count, .tgme_reaction_count')
        label = count.get_text('', strip=True) if count else reaction.get_text(' ', strip=True)
        match = re.search(r'(\d[\d\s.,]*[KMB]?)\s*$', label, re.I)
        values.append(displayed_count(match[1]) if match else None)
    if values and all(v is not None for v in values): result['likes'] = sum(values)
    return result
