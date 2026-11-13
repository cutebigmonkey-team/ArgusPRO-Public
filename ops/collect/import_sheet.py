"""Read all XLSX sheets, hyperlinks and text URLs into a reviewed source catalog.

No formulas are executed, no source sites are contacted, and no workbook is edited.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import posixpath
import re
import urllib.parse as U
import xml.etree.ElementTree as E
from zipfile import ZipFile

def source_sheet_url():
    """Optional private provenance; never embed an operational workbook ID."""
    import os
    value=os.environ.get('ARGUS_SOURCE_SHEET_URL','')
    if not value:return None
    parsed=U.urlsplit(value)
    if (parsed.scheme!='https' or parsed.hostname!='docs.google.com' or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            not re.fullmatch(r'/spreadsheets/d/[A-Za-z0-9_-]+(?:/edit)?/?',parsed.path)):
        raise ValueError('invalid_source_sheet_configuration')
    return value

NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
# These six previously published source IDs predate preservation of opaque link case.
def legacy_share_ids():
    import json,os
    value=json.loads(os.environ.get('ARGUS_LEGACY_SHARE_IDS_JSON','{}'))
    if not isinstance(value,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in value.items()):
        raise ValueError('invalid_legacy_identity_mapping')
    return value
LEGACY_SHARE_IDS=legacy_share_ids()

def rows(path):
    with ZipFile(path) as z:
        strings=[''.join(x.itertext()) for x in E.fromstring(z.read('xl/sharedStrings.xml'))] if 'xl/sharedStrings.xml' in z.namelist() else []
        rels={x.attrib['Id']:x.attrib['Target'] for x in E.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        for sheet in E.fromstring(z.read('xl/workbook.xml')).find('s:sheets',NS):
            target=rels[sheet.attrib[REL]]; p=target.lstrip('/') if target.startswith('/') else posixpath.normpath('xl/'+target)
            root=E.fromstring(z.read(p));rp=posixpath.dirname(p)+'/_rels/'+posixpath.basename(p)+'.rels'
            rel={x.attrib['Id']:x.attrib['Target'] for x in E.fromstring(z.read(rp))} if rp in z.namelist() else {}
            links={h.get('ref'):rel.get(h.get(REL),'') for h in root.findall('s:hyperlinks/s:hyperlink',NS)}
            owner=''
            for row in root.findall('s:sheetData/s:row',NS):
                cells=[]
                for c in row:
                    v=c.find('s:v',NS);inline=c.find('s:is',NS)
                    value=(v.text or '') if v is not None else ''.join(inline.itertext()) if inline is not None else ''
                    if c.get('t')=='s':value=strings[int(value)]
                    if value or c.get('r') in links:cells.append({'cell':c.get('r'),'value':value,'link':links.get(c.get('r'),'')})
                values={re.sub(r'\d+','',c['cell']):c['value'] for c in cells}
                if sheet.get('name')=='ДСНС ресурси' and values.get('A'): owner=values['A']
                if cells:yield {'sheet':sheet.get('name'),'row':int(row.get('r')),'owner':owner,'cells':cells}

def normalize(url):
    p=U.urlsplit(url.strip());host=(p.hostname or '').encode('idna').decode().lower().removeprefix('www.')
    if p.scheme not in ('http','https') or not host or p.username or p.password or p.port not in (None,80,443):raise ValueError('invalid_url')
    path=U.unquote(p.path).rstrip('/') or '/';q=U.parse_qsl(p.query,keep_blank_values=True)
    if host=='web.telegram.org':
        if re.fullmatch(r'@[A-Za-z0-9_]{3,64}',p.fragment):host='t.me';path='/'+p.fragment[1:]
        else:raise ValueError('telegram_public_username_required')
    if host=='telegram.me':host='t.me'
    if host=='twitter.com':host='x.com'
    if host=='threads.net':host='threads.com'
    if host=='t.me':
        path=re.sub(r'^/s/','/',path).lower()
        if path.startswith(('/+','/joinchat/')):raise ValueError('private_invite')
        platform='telegram'
    elif host in ('facebook.com','m.facebook.com','mbasic.facebook.com'):
        platform='facebook';host='www.facebook.com'
        if not re.search(r'/(?:share|posts|permalink|reels?|videos)/',path,re.I):path=path.lower()
        m=re.fullmatch(r'/p/.*-(\d+)',path)
        if m:path='/profile.php';q=[('id',m[1])]
    elif host in ('youtube.com','m.youtube.com','youtu.be'):platform='youtube';host='www.youtube.com' if host!='youtu.be' else host
    elif host in ('vt.tiktok.com','vm.tiktok.com'):platform='tiktok'
    elif host in ('instagram.com','x.com','tiktok.com','threads.com','vk.com','vk.ru','ok.ru','max.ru','web.max.ru','reddit.com'):
        platform={'vk.ru':'vk','vk.com':'vk','web.max.ru':'max','max.ru':'max','ok.ru':'ok','reddit.com':'reddit'}.get(host,host.split('.')[0])
        if platform=='instagram' and path.lower()=='/accounts/login':
            targets=[v for k,v in q if k=='next']
            if len(targets)!=1 or not re.fullmatch(r'/[A-Za-z0-9_.]{1,30}/?',targets[0]):
                raise ValueError('instagram_public_profile_required')
            path=targets[0].rstrip('/')
        if not re.search(r'/(?:p|reel|post|c)/',path,re.I):path=path.lower()
        if platform in ('instagram','tiktok','threads'):host='www.'+host
    elif host=='rutube.ru':platform='rutube'
    elif host=='mail.ru' or host.endswith('.mail.ru'):platform='mailru'
    elif host in ('whatsapp.com','whatsapp.net'):platform='whatsapp';host='www.whatsapp.com'
    else:platform='rss'
    # Source identity parameters, not locale or tracking. Website queries can be functional.
    if platform in ('facebook','youtube'):q=[(k,v) for k,v in q if k in ('id','story_fbid','v','list')]
    elif platform=='x' and path=='/search':
        q=[(k,v) for k,v in q if k=='q' and v.strip()]
        if len(q)!=1:raise ValueError('x_search_query_required')
    elif platform not in ('rss','mailru'):q=[]
    else:q=[(k,v) for k,v in q if not k.lower().startswith(('utm_','fbclid','yclid','gclid','__cf_'))]
    return platform,U.urlunsplit(('https',host,U.quote(path,safe="/@:!$&'()*+,;=-._~"),U.urlencode(q),''))

def extract(path):
    sources={};issues=[];sheets=collections.Counter();occurrences=0
    for row in rows(path):
        sheets[row['sheet']]+=1
        for cell in row['cells']:
            value=cell['value'];reference={'sheet':row['sheet'],'cell':cell['cell']}
            text=value.replace('\r','').replace('\n','') if value.strip().startswith('http') and value.count('http')==1 else value
            urls=re.findall(r'https?://[^\s<>"\u00a0]+',text)
            link=cell['link']
            # A visible Telegram URL carries the channel fragment that Google may drop from the hyperlink.
            if link.startswith('http') and not ('web.telegram.org' in link and urls):urls.append(link)
            if not urls:
                if re.fullmatch(r'[^\s@]+@[^\s@]+',value.strip()):issues.append({**reference,'value':value,'reason':'email_not_monitoring_url'})
                elif len(row['cells'])==1:issues.append({**reference,'value':value,'reason':'missing_url'})
                continue
            normalized=set()
            for url in set(urls):
                try:platform,canonical=normalize(url)
                except (ValueError,UnicodeError) as error:
                    issues.append({**reference,'value':url,'reason':str(error) if str(error) in ('telegram_public_username_required','private_invite') else 'invalid_url'});continue
                identity=(platform,canonical)
                if identity in normalized:continue
                normalized.add(identity);occurrences+=1
                origin='dsns' if 'ДСНС' in row['sheet'] else 'general'
                if identity not in sources:
                    source_id='sheet-'+hashlib.sha256((platform+'|'+canonical).encode()).hexdigest()[:24]
                    if platform=='facebook':
                        share_path=U.urlsplit(canonical).path
                        if share_path in LEGACY_SHARE_IDS:source_id=LEGACY_SHARE_IDS[share_path]
                        elif source_id in LEGACY_SHARE_IDS.values():
                            source_id='sheet-'+hashlib.sha256(('case-sensitive|'+canonical).encode()).hexdigest()[:24]
                    sources[identity]={'id':source_id,
                        'name':row['owner'] or U.unquote(U.urlsplit(canonical).path.strip('/')) or U.urlsplit(canonical).hostname,
                        'url':canonical,'platform':platform,'origin':origin,'enabled':True,'references':[]}
                elif row['owner']:sources[identity]['name']=row['owner']
                if reference not in sources[identity]['references']:sources[identity]['references'].append(reference)
    result={'provenance':source_sheet_url(),'workbook_sha256':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'sources':sorted(sources.values(),key=lambda s:(s['platform'],s['url']))}
    audit={'sheet_nonempty_rows':dict(sheets),'url_occurrences':occurrences,'unique_sources':len(sources),
           'duplicate_occurrences':occurrences-len(sources),'platforms':dict(collections.Counter(s['platform'] for s in sources.values())),
           'issues':issues}
    return result,audit

def is_verified_profile(source):
    return (source.get('origin') == 'general' and source.get('platform') in ('facebook','instagram','x','youtube','threads','tiktok','ok')
            and any(ref.get('kind') == 'verified_public_profile' for ref in source.get('references', [])))


def is_operator_source(source):
    return any(ref.get('kind') == 'operator_supplied_source' for ref in source.get('references', []))


def is_catalog_addition(source):
    if is_verified_profile(source) or is_operator_source(source):
        return True
    return (source.get('platform') == 'instagram' and source.get('origin') == 'general'
            and any(ref.get('kind') == 'wikidata_instagram' for ref in source.get('references', [])))


def preserve_verified_profiles(catalog, previous):
    """Keep registered sources and their provenance on workbook reimport."""
    sources = {(s['platform'], s['url']):s for s in catalog['sources']}
    for profile in previous.get('sources', []):
        if not is_catalog_addition(profile):
            continue
        evidence = [ref for ref in profile['references'] if ref.get('kind') in ('verified_public_profile','publisher_embed','operator_supplied_source','wikidata_instagram')]
        key = (profile['platform'], profile['url'])
        if key not in sources and is_operator_source(profile):
            workbook_key = normalize(profile['url'])
            if workbook_key in sources:
                key = workbook_key
        if key not in sources:
            sources[key] = profile
            continue
        source = sources[key]
        if source['origin'] != profile['origin']:
            raise ValueError('Workbook conflicts with a reviewed publisher origin: ' + profile['url'])
        source.update(id=profile['id'], name=profile['name'])
        source['references'].extend(ref for ref in evidence if ref not in source['references'])
    return {**catalog,'sources':[sources[k] for k in sorted(sources)]}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('workbook');parser.add_argument('--output',required=True);args=parser.parse_args()
    catalog,audit=extract(args.workbook)
    if Path(args.output).is_file():
        catalog=preserve_verified_profiles(catalog,json.loads(Path(args.output).read_bytes()))
    Path(args.output).write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    Path(args.output).with_name('source-import-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in audit.items() if k!='issues'},ensure_ascii=False));print('Issues:',len(audit['issues']))
