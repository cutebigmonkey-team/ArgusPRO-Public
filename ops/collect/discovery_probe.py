"""Bounded hosted search, public-HTML monitoring and archive cross-link discovery."""
from private_assets import private_asset_path
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
import types
from urllib.parse import urlsplit
import requests
from bs4 import BeautifulSoup
from cloud_social import require_cloud
from run import now, plain, source_valid, error_code
from social_links import identity, extract, from_posts, merge_records

TOOLS=Path('.discovery-tools').resolve()
MANIFEST=json.loads(Path(__file__).with_name('discovery-upstreams.json').read_text())


def change_modules():
    # Load only the reviewed, unmodified parsing/diff package. Do not initialize
    # the application's Flask server, notifications, browser or credential store.
    root=TOOLS/'changedetection/changedetectionio'
    if not root.is_dir(): raise RuntimeError('changedetection_not_installed')
    if 'changedetectionio' not in sys.modules:
        package=types.ModuleType('changedetectionio'); package.__path__=[str(root)]
        sys.modules['changedetectionio']=package
    from changedetectionio.html_tools import html_to_text
    from changedetectionio.diff import render_diff
    return html_to_text,render_diff


def snapshot(source,body,previous=None):
    from browser_public import page_error
    problem=page_error(source['url'],body)
    if problem: return None,{'status':problem}
    soup=BeautifulSoup(body,'html.parser')
    for element in soup.select('script,style,template,noscript,nav,footer,header,[hidden],[aria-hidden="true"]'):
        element.decompose()
    for element in list(soup.select('[style]')):
        if element.attrs and re.search(r'display\s*:\s*none|visibility\s*:\s*hidden',element.get('style',''),re.I): element.decompose()
    scope=soup.find('main') or soup.find('article') or soup.body or soup
    links=extract(str(scope),source['url'])
    if source['platform'] in ('facebook','instagram') and not links:
        return None,{'status':'no_visible_posts'}
    html_to_text,render_diff=change_modules()
    text='\n'.join(' '.join(line.split()) for line in html_to_text(str(scope),render_anchor_tag_content=True).splitlines() if line.strip())[:30000]
    if len(text)<40: return None,{'status':'no_visible_content'}
    digest=hashlib.sha256(text.encode()).hexdigest()
    state={'url':source['url'],'sha256':digest,'text':text,'observed_at':now(),'tool':'changedetection.io',
           'upstream_commit':MANIFEST['changedetection']['commit']}
    if previous and previous.get('upstream_commit')!=state['upstream_commit']:
        return state,{'status':'baseline_reset','reason':'parser_revision_changed','links':links}
    status='baseline_created' if not previous else 'unchanged' if previous['sha256']==digest else 'changed'
    result={'status':status,'links':links}
    if status=='changed': result['diff']=render_diff(previous['text'],text,patch_format=True)[:10000]
    return state,result


def search_rows(query,payload,limit,stamp):
    records=[]
    for row in payload.get('results',[]):
        item=identity(row.get('url'))
        if not item or item['platform']!=query['platform']: continue
        snippet=plain(row.get('content',''))[:2000]
        evidence={'method':'searxng','query':query['q'],'engines':[str(e)[:60] for e in row.get('engines',[])[:5]],
                  'upstream_commit':MANIFEST['searxng']['commit'],'association':'query_match_only'}
        for field in ('source_id','source_url'):
            if field in query:evidence[field]=query[field]
        records.append({**item,'status':'search_snippet' if snippet else 'link_only','content_status':'not_fetched',
            'text':'','published_at':None,'observed_at':stamp,'title':plain(row.get('title',''))[:500],
            'snippet':snippet,'evidence':[evidence]})
        if len(records)>=limit: break
    return records


def start_search(root):
    settings={'use_default_settings':{'engines':{'keep_only':['google','bing','duckduckgo']}},
        'general':{'debug':False,'enable_metrics':False},
        'server':{'secret_key':secrets.token_hex(32),'limiter':False,'public_instance':False},
        'search':{'formats':['json'],'autocomplete':'','default_lang':'uk-UA'},
        'outgoing':{'request_timeout':8.0,'max_request_timeout':10.0},
        'engines':[{'name':n,'disabled':False,'timeout':8.0} for n in ('google','bing','duckduckgo')]}
    config=root/'settings.yml'; config.write_text(json.dumps(settings),encoding='utf-8')
    home=root/'home'; home.mkdir(exist_ok=True)
    env={k:os.environ[k] for k in ('PATH','LANG','LD_LIBRARY_PATH') if k in os.environ}
    env.update(HOME=str(home),SEARXNG_SETTINGS_PATH=str(config),SEARXNG_DEBUG='false')
    logfile=(root/'searxng.log').open('wb')
    process=subprocess.Popen([sys.executable,'-m','gunicorn','--bind','127.0.0.1:8888','--workers','1','--threads','2',
        '--timeout','35','searx.webapp:app'],cwd=TOOLS/'searxng',env=env,stdout=logfile,stderr=logfile,start_new_session=True)
    logfile.close()
    return process


def query_pages(query,cursors,read_page,*,budget=3,source_candidates=None):
    import hashlib
    language=query.get('language','uk-UA')
    key=hashlib.sha256(json.dumps([query['platform'],query['q'],language],ensure_ascii=False).encode()).hexdigest()
    previous=cursors.get(key,{})
    page=previous.get('next',1)
    seen=previous.get('signature')
    empty=previous.get('empty',0)
    accepted={};pages=[];status='page_budget'
    for _ in range(budget):
        try:
            payload=read_page(query['q'],language,page)
            results=payload['results']
            if not isinstance(results,list):raise ValueError('invalid_search_response')
            failed=payload.get('unresponsive_engines',[])
            rows=search_rows(query,payload,len(results),now()) if results else []
            if source_candidates is not None:
                from source_discovery import public_profiles
                source_candidates.extend(public_profiles((r.get('url','') for r in results), 'search_result', query['q'], now()))
            for row in rows:accepted[row['canonical_key']]=row
            pages.append({'page':page,'results':len(results),'accepted':len(rows),'unresponsive_engines':failed})
            if failed and not results:
                status='search_unavailable'
                break
            if failed:status='partial_page_budget'
            signature=hashlib.sha256(json.dumps(sorted(str(row.get('url','')) for row in results)).encode()).hexdigest()
            empty=0 if results else empty+1
            if empty>=2 or (results and signature==seen) or page>=200:
                status='empty_confirmed' if empty>=2 else 'repeated_page' if signature==seen else 'depth_target'
                cursors.pop(key,None)
                break
            page+=1
            seen=signature
            cursors[key]={'next':page,'signature':seen,'empty':empty}
        except Exception as exc:
            status='search_failed'
            pages.append({'page':page,'error_type':type(exc).__name__})
            break
    return list(accepted.values()),{**query,'status':status,'accepted':len(accepted),'pages':pages,'next_page':cursors.get(key,{}).get('next')}


def searches(config,disabled,root,cursors,source_candidates=None):
    rows=[]; reports=[]; process=start_search(root)
    try:
        ready=False
        with requests.Session() as session:
            session.trust_env=False
            for _ in range(40):
                if process.poll() is not None: break
                try:
                    ready=session.get('http://127.0.0.1:8888/healthz',timeout=1).status_code==200
                except requests.RequestException: pass
                if ready: break
                time.sleep(1)
            if not ready: raise RuntimeError('searxng_start_failed')
            engines={'google','bing','duckduckgo'}
            def read_page(q,language,page):
                if not engines:raise RuntimeError('search_engines_unavailable')
                time.sleep(2)
                with session.get('http://127.0.0.1:8888/search',params={'q':q,'format':'json','pageno':page,'language':language,
                        'engines':','.join(sorted(engines)),'safesearch':0},timeout=20,allow_redirects=False,stream=True) as response:
                    response.raise_for_status();raw=bytearray()
                    for chunk in response.iter_content(65536):
                        raw.extend(chunk)
                        if len(raw)>2*1024**2:raise ValueError('search_response_too_large')
                payload=json.loads(raw)
                for failure in payload.get('unresponsive_engines',[]):
                    if isinstance(failure,list) and failure:engines.discard(failure[0])
                return payload
            for query in config['queries'][:16]:
                if query['platform'] in disabled:continue
                accepted,report=query_pages(query,cursors,read_page,budget=config['pages_per_query_per_run'],source_candidates=source_candidates)
                rows.extend(accepted);reports.append(report)
    finally:
        if process.poll() is None:
            os.killpg(process.pid,signal.SIGTERM)
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: os.killpg(process.pid,signal.SIGKILL); process.wait()
    return rows,reports


def main():
    require_cloud()
    from registry import validate
    from remaining_http import PublicHTTP,StopCollection
    from run import fetch
    root=Path('collection/discovery').resolve(); root.mkdir(parents=True,exist_ok=True)
    config=json.loads(private_asset_path('discovery-config.json').read_text(encoding='utf-8'))
    input_data=json.loads((root/'input.json').read_text(encoding='utf-8'))
    registry=validate(json.loads(Path('registry/collectors.json').read_text()))
    disabled={r['platform'] for r in registry['collectors'] if not r['enabled']}
    incoming=from_posts(input_data['posts'],disabled)
    reports={'archive_posts_scanned':len(input_data['posts']),'cross_links':len(incoming)}
    catalog=json.loads(Path(__file__).with_name('sources.json').read_text(encoding='utf-8'))['sources']
    from source_discovery import scan, merge_candidates, mentioned_profiles, source_counts
    from source_origin import catalog_url
    catalog += input_data.get('registered_discoveries', [])
    cooldowns=input_data.get('discovery_cooldowns',{})
    validation_cursors=input_data.get('validation_cursors',{})
    candidates, site_cursor, site_checks = scan(catalog, input_data.get('site_cursor'), disabled,cooldowns=cooldowns,
                                               directory_sites=input_data.get('directory_sites',[]),validation_cursors=validation_cursors)
    for post in input_data['posts']:
        candidates.extend(mentioned_profiles(post))
    search_cursor=input_data.get('search_cursor')
    if config.get('catalog_queries'):
        from discovery_plan import plan
        queries,search_cursor=plan(catalog,search_cursor,disabled,min(16,config.get('queries_per_run',16)),
                                  configured=config['queries'],languages=config['languages'],publisher_queries=True)
        config={**config,'queries':queries}
        reports['search_scope']={**search_cursor,'selected':len(queries)}
    try:
        indexed,report=searches(config,disabled,root,(search_cursor or {}).setdefault('continuations',{}), candidates); incoming+=indexed; reports['searches']=report
        if config.get('catalog_queries'):
            from discovery_plan import advance
            search_cursor=advance(search_cursor,len(report))
    except Exception as error:
        reports['searches']=[{'status':'service_failed','error_type':type(error).__name__}]
    by_url={s['url'].rstrip('/'):s for s in catalog if s.get('enabled',True) and source_valid(s)}
    monitors=[]; baselines={}; paused=set()
    for url in config['watch_urls'][:8]:
        source=by_url.get(url.rstrip('/'))
        if not source:
            monitors.append({'url':url,'status':'not_in_enabled_catalog'}); continue
        if source['platform'] in disabled or source['platform'] in paused:
            monitors.append({'url':url,'status':'disabled_or_rate_limited'}); continue
        try:
            if source['platform'] in ('facebook','instagram'):
                body=PublicHTTP(source['platform'],budget=4).request(url)
            else:
                raw,final=fetch(url,timeout=10)
                if urlsplit(final).hostname!=urlsplit(url).hostname: raise ValueError('unexpected_redirect')
                body=raw.decode('utf-8',errors='replace')
            baseline,result=snapshot(source,body,input_data['baselines'].get(url))
            monitors.append({'url':url,**result})
            if baseline:
                baselines[url]=baseline
                for target in result['links']:
                    found=identity(target)
                    if found and found['platform'] not in disabled:
                        incoming.append({**found,'status':'link_only','content_status':'not_fetched','text':'',
                            'published_at':None,'observed_at':now(),'evidence':[{'method':'public_html','source_url':url}]})
        except StopCollection as error:
            monitors.append({'url':url,'status':error.status})
            if error.status=='http_429': paused.add(source['platform'])
        except Exception as error: monitors.append({'url':url,'status':error_code(error),'error_type':type(error).__name__})
        time.sleep(1)
    records=merge_records([],incoming)
    reports.update(format='argus-social-discovery-report-v1',run_id=os.environ['GITHUB_RUN_ID'],updated_at=now(),
                   discovered_links=len(records),monitors=monitors,upstreams=MANIFEST)
    known = {catalog_url(s['url']) for s in catalog}
    candidates = merge_candidates([], [r for r in candidates if r['platform'] not in disabled and
                                       catalog_url(r['url']) not in known])
    reports.update(publisher_site_checks=site_checks, candidate_counts=source_counts(candidates))
    output={'records':records,'baselines':baselines,'report':reports,'search_cursor':search_cursor,
            'source_candidates':candidates,'site_cursor':site_cursor,'discovery_cooldowns':cooldowns,
            'validation_cursors':validation_cursors}
    (root/'result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'links':len(records),'cross_links':reports['cross_links'],'searches':reports['searches'],
                      'monitors':[{k:r[k] for k in ('url','status')} for r in monitors]},ensure_ascii=True))


if __name__=='__main__': main()
