"""Private incremental descriptive statistics. No AI calls or synthetic values.

Build the in-memory contributions once from the verified archive at worker start;
subsequent feeds replace contributions only for changed IDs. Publish immutable
bytes, verify them, then advance a separate pointer. Failure leaves the previous
snapshot available and never prevents archive delivery.
"""
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import hashlib
import itertools
import json
from drive_store import encoded, MAX_JSON

POINTER='analytics-catalog.json'

def metric(value):
    return value if type(value) is int and value>=0 else None

def normalize(item, source=None):
    source=source or {}
    from engagement_metrics import counters
    return {**item, 'url':item.get('url') or item.get('post_url') or '', 'source_id':item.get('source_id') or '',
        'source_name':item.get('source_name') or source.get('name') or '',
        'category':item.get('category') or source.get('category') or '',
        'region':item.get('region') or source.get('region') or '',
        'title':item.get('title') or '', 'content':item.get('text') or item.get('content') or '',
        'collected_at':item.get('collected_at') or item.get('observed_at'),
        'engagement':counters(item),
        'keywords':list(dict.fromkeys(k for k in item.get('keywords',[]) if isinstance(k,str)))
                   if isinstance(item.get('keywords'),list) else [],
        'language':item.get('language') or ''}

FIELDS=('origin','platform','category','region','day','source','views','comments','interactions','interactionsComplete')
FIELD_INDEX={field:i for i,field in enumerate(FIELDS)}

class Contribution(tuple):
    """Compact immutable analytics projection; never retain an archive body."""
    __slots__=()
    def __getitem__(self,key):
        return tuple.__getitem__(self,FIELD_INDEX[key] if isinstance(key,str) else key)

from functools import lru_cache

@lru_cache(maxsize=4096)
def dimension(value):
    return value


def contribution(item, now, source=None):
    if item.get('sample'):return None
    stamp=item.get('published_at');day=''
    if stamp:
        try:
            time=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if time.tzinfo is None:stamp=None
            elif time.timestamp()>now:return None
            else:day=time.astimezone(ZoneInfo('Europe/Kyiv')).date().isoformat()
        except (ValueError,TypeError):stamp=None
    p=normalize(item,source); e=p['engagement']
    source=p['source_id'] or p.get('source_url') or p['source_name']
    if not source and p['platform'] in ('rss','web','website'):
        from urllib.parse import urlsplit
        source=urlsplit(p.get('url','')).hostname or ''
    return Contribution((p.get('origin') if p.get('origin') in ('general','dsns') else 'unknown',
        p['platform'],p['category'],p['region'],dimension(day),
        dimension(p['platform']+':'+source) if source else '',e['views'],e['comments'],
        sum(v for v in (e['likes'],e['comments']) if v is not None)
            if e['likes'] is not None or e['comments'] is not None else None,
        e['likes'] is not None and e['comments'] is not None))

def keys(row):
    return itertools.product(*[("",row[k]) if row[k] else ("",) for k in ('origin','platform','region','category')])

class AnalyticsIndex:
    def __init__(self,items,sources=()):
        self.sources={(p['platform'],p['id']):p for p in sources}
        self.rows={}; self.groups={};self.published=None;self.revision=0;self.future={};self.now=datetime.now(timezone.utc).timestamp()
        self.update(items)

    def adjust(self,row,sign):
        for key in keys(row):
            group=self.groups.get(key)
            if group is None:
                group={'counts':Counter(),'sources':Counter(),'days':Counter(),'platforms':Counter(),'regions':Counter()}
                self.groups[key]=group
            c=group['counts'];c['posts']+=sign;c['undated']+=sign*int(not row['day'])
            for metric in ('views','comments','interactions'):
                value=row[metric]
                if value is not None:c[metric]+=sign*value;c[metric+'Known']+=sign
            c['interactionsComplete']+=sign*int(row['interactionsComplete'])
            for field,value in [('sources',row['source']),('days',row['day']),('platforms',row['platform']),('regions',row['region'])]:
                if value:
                    group[field][value]+=sign
                    if not group[field][value]:del group[field][value]
            if not c['posts']:del self.groups[key]

    def update(self,items):
        changed=0;self.now=datetime.now(timezone.utc).timestamp()
        # Disk-backed archive iterators must not be materialized into a dict.
        for item in itertools.chain(tuple(self.future.values()),items):
            row=contribution(item,self.now,self.sources.get((item['platform'],item.get('source_id'))));old=self.rows.get(item['id'])
            if row is None and not item.get('sample'):
                self.future[item['id']]={k:v for k,v in item.items() if k in
                    ('id','platform','origin','source_id','source_url','source_name','category','region',
                     'published_at','url','engagement','likes','views','comments_count')}
            else:self.future.pop(item['id'],None)
            if old==row:continue
            if old:self.adjust(old,-1)
            if row:self.adjust(row,1);self.rows[item['id']]=row
            else:self.rows.pop(item['id'],None)
            changed+=1
        self.revision+=changed
        return changed

    def summaries(self):
        result={}
        for key,g in self.groups.items():
            c=g['counts'];n=c['posts']
            result[json.dumps(key,separators=(',',':'),ensure_ascii=False)]={
                'totalPosts':n,'uniqueSources':len(g['sources']),'undatedPosts':c['undated'],
                **{out:c[metric] if c[metric+'Known'] else None for out,metric in
                   [('totalViews','views'),('totalComments','comments'),('totalInteractions','interactions')]},
                'metricCoverage':{metric:{'knownPosts':c[metric+'Known'],'totalPosts':n,
                    'completePosts':c['interactionsComplete'] if metric=='interactions' else c[metric+'Known']}
                    for metric in ('views','comments','interactions')},
                'postsByDay':[{'date':day,'posts':count} for day,count in sorted(g['days'].items())],
                'platformBreakdown':[{'platform':p,'count':n} for p,n in sorted(g['platforms'].items(),key=lambda x:(-x[1],x[0]))],
                'regionBreakdown':[{'region':r,'count':n} for r,n in sorted(g['regions'].items())]}
        return result

    def publish(self,store,base,head,updated_at):
        self.update([])
        version=(base['snapshot_sha256'],(head or {}).get('sha'),self.revision)
        if version==self.published:return False
        store.assert_private()
        value={'format':'argus-analytics-v1','base_sha256':version[0],'live_sha256':version[1],
               'live_sequence':(head or {}).get('last',0),'archive_updated_at':updated_at,
               'generated_at':datetime.now(timezone.utc).isoformat(),'summaries':self.summaries()}
        raw=encoded(value)
        if len(raw)>MAX_JSON:raise ValueError('analytics_snapshot_too_large')
        digest=hashlib.sha256(raw).hexdigest();name='analytics-'+digest+'.json'
        file_id=store.put(name,raw)
        if store.read_bytes(name,MAX_JSON)!=raw:raise ValueError('analytics_readback_failed')
        pointer={'format':'argus-analytics-catalog-v1','id':file_id,'sha256':digest,
                 'base_sha256':version[0],'live_sha256':version[1],'live_sequence':value['live_sequence'],
                 'generated_at':value['generated_at']}
        store.json(POINTER,pointer,mutable=True)
        if store.read_json(POINTER)!=pointer:raise ValueError('analytics_pointer_readback_failed')
        self.published=version
        return True
