"""Backend-only SQL statistics and evidence references, never generated facts."""
import datetime as dt
import itertools
import re
import sqlite3
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

KYIV = ZoneInfo('Europe/Kyiv')
REVIEW = '— [перевірити]'


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, TypeError): return None


def source(item):
    # Do not count every article URL as a separate source, or every platform
    # as one source. Unknown sources remain unknown.
    value = item.get('source_id') or item.get('source_url') or item.get('source_name')
    if not value:
        url = urlsplit(item.get('url') or '')
        host = url.hostname or ''
        path = url.path.strip('/').split('/')
        if host in ('t.me', 'telegram.me', 'www.facebook.com', 'facebook.com', 'www.instagram.com', 'instagram.com') and path[0]:
            value = host + '/' + path[0]
        elif item.get('platform') in ('rss', 'web', 'website'): value = host
    return item.get('platform', '') + ':' + value if value else None


def metric(item, key):
    value = item.get(key)
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def build_reports(snapshot, assessments, generated_at, extra_requests=()):
    now = timestamp(generated_at)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('''CREATE TABLE posts(id TEXT PRIMARY KEY, platform TEXT, region TEXT, official INTEGER,
        source TEXT, published REAL, mention INTEGER, sentiment TEXT, topic TEXT,
        risk TEXT, relevance INTEGER, critical INTEGER, complaint INTEGER, resonance INTEGER,
        likes INTEGER, comments INTEGER, views INTEGER, assessed INTEGER)''')
    for p in snapshot['items']:
        if p.get('sample'): continue
        result = assessments.get(p['id'], {})
        values = result.get('assessments', {}) if result.get('status') == 'completed' else {}
        value = lambda key: values.get(key, {}).get('value')
        conn.execute('INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
            p['id'], p['platform'], p.get('region', ''), int(p.get('origin') == 'dsns'), source(p), timestamp(p.get('published_at')),
            int(bool(re.search(r'(?<!\w)ДСНС(?!\w)', p.get('text', '')))),
            value('sentiment'), value('topic'), value('risk'), value('relevance'), value('critical'), value('complaint'), value('resonance'),
            metric(p, 'likes'), metric(p, 'comments_count'), metric(p, 'views'), int(bool(values))))
    conn.execute('CREATE INDEX by_period ON posts(published, official, platform, region)')
    # Most report combinations constrain region/platform. A time-first index
    # otherwise repeats broad scans for every combination on the small worker.
    conn.execute('CREATE INDEX by_platform ON posts(platform, published)')
    conn.execute('CREATE INDEX by_region ON posts(region, platform, published)')
    platforms = ['all'] + [r[0] for r in conn.execute('SELECT DISTINCT platform FROM posts ORDER BY platform')]
    regions = ['all'] + [r[0] for r in conn.execute("SELECT DISTINCT region FROM posts WHERE region <> '' ORDER BY region")]
    day_start = dt.datetime.fromtimestamp(now, KYIV).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    periods = {'today': day_start, '24h': now-86400, '48h': now-172800, '7d': now-604800, '30d': now-2592000, 'all': None}
    requests = [dict(period=period, platform=platform, region=region, dsns=dsns, start=start, end=now)
        for (period, start), platform, region, dsns in itertools.product(periods.items(), platforms, regions, (False, True))]
    for request in extra_requests:
        start, end = timestamp(request.get('from')), timestamp(request.get('to'))
        if start is None or end is None or start > end or end > now: raise ValueError('Invalid custom report period')
        requests.append(dict(period='custom', platform=request.get('platform', 'all'), region=request.get('region', 'all'),
            dsns=bool(request.get('dsns')), start=start, end=end, customFrom=request['from'], customTo=request['to']))
    if len(requests) > 5000: raise ValueError('Report dimension limit; narrow archive dimensions before expanding')
    reports = []
    for request in requests:
        where, params = [], []
        if request['start'] is not None:
            where += ['published >= ?', 'published <= ?']; params += [request['start'], request['end']]
        else:
            where += ['(published IS NULL OR published <= ?)']; params += [request['end']]
        if request['dsns']: where += ['official = 1']
        for field in ('platform', 'region'):
            if request[field] != 'all': where += [field + ' = ?']; params += [request[field]]
        clause = ' WHERE ' + ' AND '.join(where)
        row = dict(conn.execute('''SELECT COUNT(*) AS posts, COUNT(DISTINCT source) AS sources,
            SUM(source IS NULL) AS unknown_sources, SUM(published IS NULL) AS undated,
            SUM(assessed) AS assessed, SUM(mention) AS mentions,
            SUM(sentiment='positive') AS positive, SUM(sentiment='neutral') AS neutral,
            SUM(sentiment='negative') AS negative, SUM(relevance=1) AS relevant,
            SUM(critical=1) AS critical, SUM(complaint=1) AS complaints, SUM(resonance=1) AS resonance,
            SUM(risk='high') AS high_risk, SUM(likes) AS likes, SUM(comments) AS comments, SUM(views) AS views,
            COUNT(likes) AS likes_known, COUNT(comments) AS comments_known, COUNT(views) AS views_known
            FROM posts''' + clause, params).fetchone())
        for key in ('assessed', 'mentions', 'positive', 'neutral', 'negative', 'relevant', 'critical', 'complaints', 'resonance', 'high_risk', 'unknown_sources', 'undated'):
            row[key] = row[key] or 0
        # Unknown engagement is NULL, not an invented zero or estimated reach.
        for key in ('likes', 'comments', 'views'):
            if row[key + '_known'] != row['posts'] or not row['posts']: row[key] = None
        evidence = {}
        for name, condition in {'top': 'relevance=1', 'positive': "sentiment='positive'", 'negative': "sentiment='negative'",
                                'critical': 'critical=1', 'complaints': 'complaint=1', 'resonance': 'resonance=1',
                                'innovation': "topic='innovation'", 'partners': "topic='partners'"}.items():
            evidence[name] = [r[0] for r in conn.execute('SELECT id FROM posts' + clause + ' AND ' + condition + ' ORDER BY published DESC, id LIMIT 5', params)]
        platforms_rows = [dict(r) for r in conn.execute('SELECT platform, COUNT(*) AS posts FROM posts' + clause + ' GROUP BY platform ORDER BY platform', params)]
        topics = [dict(r) for r in conn.execute("SELECT topic, COUNT(*) AS count FROM posts" + clause + " AND topic IS NOT NULL GROUP BY topic ORDER BY count DESC, topic LIMIT 5", params)]
        for topic in topics:
            topic['post_id'] = conn.execute('SELECT id FROM posts' + clause + ' AND topic=? ORDER BY published DESC, id LIMIT 1', params + [topic['topic']]).fetchone()[0]
        row['engagement'] = row['likes'] + row['comments'] if row['likes'] is not None and row['comments'] is not None else None
        classified = row['positive'] + row['neutral'] + row['negative']
        row['negative_percent'] = round(100 * row['negative'] / classified, 1) if classified else None
        row['positive_neutral_percent'] = round(100 * (row['positive'] + row['neutral']) / classified, 1) if classified else None
        row['sentiment_classified'] = classified
        reports.append({'filters': request, 'statistics': row, 'evidence': evidence, 'platforms': platforms_rows, 'topics': topics,
                        'generated_at': generated_at, 'status': 'draft',
                        'note': 'Кількість публікацій не є кількістю подій. Оцінки AI потребують перевірки; джерела не є незалежним підтвердженням.'})
    conn.close()
    return reports
