"""Rotate search over every enabled Meta source, with a committed cloud cursor."""
import hashlib
import itertools
import json
import re
from urllib.parse import urlsplit,parse_qs


def plan(catalog,previous=None,disabled=(),budget=16,*,configured=(),languages=('uk-UA',),publisher_queries=False):
    from run import source_valid
    groups={'instagram':[],'facebook':[]}; invalid=[]
    for source in sorted(catalog,key=lambda s:s['id']):
        platform=source['platform']
        if platform not in groups or platform in disabled or not source.get('enabled',True):continue
        if not source_valid(source):invalid.append(source['id']);continue
        path=urlsplit(source['url']).path.strip('/')
        if platform=='instagram':
            if re.fullmatch(r'[A-Za-z0-9_.]{1,30}',path): q='site:instagram.com/p/ "'+path+'"'
            elif re.fullmatch(r'(?:p|reel)/[A-Za-z0-9_-]+',path):q='site:instagram.com/'+path
            else:invalid.append(source['id']);continue
        else:
            if path=='profile.php':
                owner=parse_qs(urlsplit(source['url']).query).get('id',[''])[0]
                if not owner.isdigit():invalid.append(source['id']);continue
                q='site:facebook.com "'+owner+'"'
            elif re.fullmatch(r'[A-Za-z0-9_.-]+',path):q='site:facebook.com/'+path+'/posts/'
            elif re.fullmatch(r'groups/[A-Za-z0-9_.-]+',path):q='site:facebook.com/'+path+'/posts/'
            else:q='site:facebook.com/'+path
        groups[platform].append({'platform':platform,'q':q,'source_id':source['id'],'source_url':source['url']})
    configured=[q for q in configured if q['platform'] not in disabled and q.get('enabled',True)]
    # Mix standalone configured topics and source-scoped searches; previously
    # catalog mode replaced the configured topics completely.
    terms={p:sorted({w for q in configured if q['platform']==p for w in re.findall(r'"([^"]+)"',q['q'])}) for p in groups}
    owners=[q for pair in itertools.zip_longest(groups['instagram'],groups['facebook']) for q in pair if q]
    combos=[{**q,'q':q['q']+' "'+term+'"'} for q in owners for term in terms[q['platform']] if term not in q['q']]
    profiles=[]
    if publisher_queries:
        # Search by catalogued public publisher name. Results still require separate identity evidence.
        domains={'telegram':'t.me','youtube':'youtube.com','instagram':'instagram.com','facebook':'facebook.com',
                 'tiktok':'tiktok.com','threads':'threads.com','x':'x.com','rutube':'rutube.ru','ok':'ok.ru','vk':'vk.com'}
        names=sorted({s['name'].strip() for s in catalog if s.get('enabled',True) and s.get('name','').strip()})
        profiles=[{'platform':p,'q':f'site:{domain} "{name[:120]}"'} for name in names
                  for p,domain in domains.items() if p not in disabled]
    queries=[{**q,'language':lang} for segment in itertools.zip_longest(configured,owners,combos,profiles)
             for q in segment if q for lang in languages]
    digest=hashlib.sha256(json.dumps(queries,sort_keys=True).encode()).hexdigest()
    previous=previous or {}; start=previous.get('offset',0)
    if previous.get('catalog_sha256')!=digest and previous.get('resume_query') in queries:
        start=queries.index(previous['resume_query'])
    if type(start) is not int or not 0<=start<max(1,len(queries)):start=0
    selected=queries[start:start+budget]
    return selected,{'catalog_sha256':digest,'offset':start,'total':len(queries),'invalid_source_ids':invalid,
                     'batch_queries':selected,'following_query':queries[(start+len(selected))%len(queries)] if queries else None,
                     'continuations':previous.get('continuations',{}) if previous.get('catalog_sha256')==digest else {}}


def advance(cursor,processed):
    cursor=dict(cursor); total=cursor['total']
    batch=cursor.pop('batch_queries',[])
    following=cursor.pop('following_query',None)
    cursor['resume_query']=batch[processed] if processed<len(batch) else following
    cursor['offset']=(cursor['offset']+processed)%total if total else 0
    return cursor
