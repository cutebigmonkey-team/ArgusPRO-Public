"""Cloud worker for public profile embeds, RSS-Bridge and gallery metadata."""
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit

from remaining_http import PublicHTTP, StopCollection
from remaining_social import owner
from remaining_worker import facebook_permalink

UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'


def php_parse(tool, tools, body, work):
    page, request, output = work/'page.html', work/'php.json', work/'parsed.json'
    page.write_text(body,encoding='utf-8')
    request.write_text(json.dumps({'html':str(page),'tool':tool,'tools':str(tools)}),encoding='utf-8')
    subprocess.run(['php',str(Path(__file__).with_name('embed_parser.php')),str(request),str(output)],
                   check=True,timeout=12,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return json.loads(output.read_text(encoding='utf-8'))


def instagram_rows(source, data):
    expected = owner(source['url'])
    profile = data.get('profile') or {}
    if str(profile.get('user','')).lower() != expected:
        return []
    rows = []
    for p in profile.get('posts',[])[:20]:
        code = p.get('code','')
        author = str(p.get('owner','')).lower()
        if not re.fullmatch(r'[A-Za-z0-9_-]{5,40}',code):
            continue
        if expected != author and expected not in p.get('coauthors',[]):
            continue
        media = ['https://www.instagram.com/p/'+m['code']+'/media/?size=l'
                 for m in p.get('media',[]) if re.fullmatch(r'[A-Za-z0-9_-]{5,40}',str(m.get('code','')))]
        rows.append({'url':'https://www.instagram.com/p/'+code+'/', 'source_owner':expected,
                     'text':p.get('caption',''),'published':p.get('date'),'media':media})
    return rows


def embed_diagnostics(source, data, body):
    profile = data.get('profile')
    return {'response_bytes':len(body.encode('utf-8')),
            'context_marker':'"contextJSON"' in body,
            'context_null':bool(re.search(r'"contextJSON"\s*:\s*null',body)),
            'profile_parsed':isinstance(profile,dict),
            'profile_matches':isinstance(profile,dict) and str(profile.get('user','')).lower()==owner(source['url']),
            'parsed_posts':len((profile or {}).get('posts',[]))}


def facebook_rows(source, data):
    from run import HOSTS, IDENTITIES
    expected = owner(source['url'])
    canonical = data.get('canonical') or ''
    aliases = {expected}
    if urlsplit(canonical).hostname in HOSTS['facebook'] and owner(canonical):
        aliases.add(owner(canonical))
    rows = []
    requested = re.search(IDENTITIES['facebook'], source['url'])
    for p in data.get('items',[])[:20]:
        url = p.get('uri') or ''
        actors = p.get('actors') or []
        proven = owner(url) in aliases
        for actor in actors:
            actor_url = actor.get('url') or ''
            if (urlsplit(actor_url).hostname in HOSTS['facebook'] and owner(actor_url) in aliases) or str(actor.get('id')) in aliases:
                proven = True
                url = facebook_permalink(url,p.get('uid'),actor.get('id'))
                break
        if not proven and not requested:
            continue
        # RSS-Bridge's generated title and profile avatar are not post content.
        rows.append({'url':url,'source_owner':expected,'text':p.get('content') or '',
                     'published':p.get('timestamp'),'media':[u for u in p.get('enclosures',[]) if u]})
    return rows


def gallery_row(source, metadata, media_url, resolved_ids):
    platform, expected = source['platform'], owner(source['url'])
    if platform == 'instagram':
        author = str(metadata.get('username') or '').lower()
        if author != expected and not re.search(r'/(p|reel)/',source['url']):
            return None
        url = metadata.get('post_url') or ''
    else:
        photo = str(metadata.get('id') or '')
        uid = str(metadata.get('user_id') or '')
        from run import IDENTITIES
        direct = re.search(IDENTITIES['facebook'],source['url'])
        if not re.fullmatch(r'\d+',photo) or (not direct and uid not in resolved_ids):
            return None
        url = 'https://www.facebook.com/photo/?'+urlencode({'fbid':photo})
    stamp = metadata.get('post_date') or metadata.get('date')
    if isinstance(stamp,dt.datetime):
        stamp = stamp.replace(tzinfo=stamp.tzinfo or dt.timezone.utc).isoformat()
    return {'url':url,'source_owner':expected,'text':metadata.get('description') or metadata.get('caption') or '',
            'published':stamp,'media':[media_url]}


def gallery(source, tools, http, work, limit=5):
    # Reuse the actual web wrapper's anonymous configuration and empty-profile
    # handling, without its web server, cookie storage or downloading worker.
    sys.path.insert(0,str(tools/'gallery-dl-web/backend/src'))
    from gallery_dl import config, extractor
    from gallery_dl.extractor.common import Extractor, Message
    from gallery_dl.extractor.facebook import FacebookExtractor
    from gallery_dl_web.gallerydl import config_builder, upstream_patches, errors
    import requests

    config.clear()
    config_builder.apply({'platform':source['platform'],'output_dir':str(work),'anonymous':True,
        'cookies':None,'options':{'include':'posts' if source['platform']=='instagram' else 'photos',
        'fallback-retries':0,'sleep-request':2.0,'sleep':0.0}},config)
    for key,value in {'retries':0,'timeout':12,'max-posts':limit,'netrc':False,'cookies':None,
                      'cookies-from-browser':None,'username':None,'password':None}.items():
        config.set(('extractor',),key,value)
    upstream_patches.install()
    resolved = set()
    original_profile = FacebookExtractor._extract_profile
    original_request = Extractor.request
    def profile(self, target, set_id=False):
        data = original_profile(self,target,set_id)
        if str(target).lower() == owner(source['url']) and data.get('id'):
            resolved.add(str(data['id']))
        return data
    FacebookExtractor._extract_profile = profile
    def request(self, url, method='GET', **kwargs):
        params = kwargs.get('params')
        if params:
            url += ('&' if '?' in url else '?') + (params if isinstance(params,str) else urlencode(params,doseq=True))
        headers = dict(self.session.headers) if self.session else {}
        headers.update(kwargs.get('headers') or {})
        payload = kwargs.get('data')
        if 'json' in kwargs:
            payload = json.dumps(kwargs['json'])
            headers['Content-Type'] = 'application/json'
        body = http.request(url,method,headers,payload)
        response = requests.Response()
        response.status_code = 200
        response.url = url
        response.encoding = 'utf-8'
        response._content = body.encode('utf-8')
        return response
    Extractor.request = request
    platform = source['platform']
    from run import IDENTITIES
    url = source['url']
    if not re.search(IDENTITIES[platform],url):
        name = owner(url)
        if '/' in name:
            raise StopCollection('unsupported_source_shape')
        url = ('https://www.instagram.com/'+name+'/posts/' if platform=='instagram'
               else 'https://www.facebook.com/'+name+'/photos')
    parser = extractor.find(url)
    if parser is None or parser.category != platform:
        raise StopCollection('unsupported_source_shape')
    rows, reason = {}, None
    try:
        for index,message in enumerate(parser):
            if index >= 1000:
                reason = 'item_budget'; break
            if message[0] != Message.Url:
                continue # no avatar/info/queue conversion and no media downloads
            row = gallery_row(source,dict(message[2]),message[1],resolved)
            if not row:
                continue
            key = row['url']
            if key in rows:
                rows[key]['media'] = list(dict.fromkeys(rows[key]['media']+row['media']))[:12]
            else:
                rows[key] = row
            if len(rows) >= limit:
                break
    except StopCollection as error:
        reason = error.status
    except Exception as error:
        message = str(error)
        if errors.detect_login_wall([message],anonymous=True):
            reason = 'login_required'
        elif errors.detect_rate_limit([message]):
            reason = 'http_429'
        else:
            reason = 'extraction_failed'
    Extractor.request = original_request
    FacebookExtractor._extract_profile = original_profile
    upstream_patches.uninstall()
    return list(rows.values()), reason


def main():
    from cloud_social import require_cloud, STOP
    require_cloud()
    request, output = map(Path,sys.argv[1:])
    job = json.loads(request.read_text())
    source, tool, tools = job['source'],job['tool'],Path(job['tools'])
    # Public embeds/prefetched posts may coexist with a sign-in form. Parse that
    # already public payload before declaring an HTML-only login wall. Explicit
    # HTTP restrictions, login redirects and API access errors always stop.
    http = PublicHTTP(source['platform'],budget=8 if tool=='gallery-dl' else 4,check_html_login=tool=='gallery-dl')
    try:
        reason = None
        diagnostics = None
        if tool == 'gallery-dl':
            rows, reason = gallery(source,tools,http,request.parent,job.get('limit',5))
        else:
            url = source['url'].rstrip('/') + ('/embed/' if tool=='instagramprofiles' else '/')
            # Profiles only: do not append path components after query strings.
            if tool == 'rss-bridge':
                url = source['url']
            body = http.request(url,headers={'User-Agent':UA,'Accept-Language':'en-US,en;q=0.9'})
            data = php_parse(tool,tools,body,request.parent)
            rows = instagram_rows(source,data) if tool=='instagramprofiles' else facebook_rows(source,data)
            if tool=='instagramprofiles':
                diagnostics = embed_diagnostics(source,data,body)
                if not diagnostics['profile_parsed']:
                    reason = 'embed_data_unavailable' if diagnostics['context_null'] else 'unrecognized_embed' if diagnostics['context_marker'] else 'no_embed_data'
                elif not diagnostics['profile_matches']:
                    reason = 'profile_mismatch'
            if not rows and any(x in body.lower() for x in ('name="login"','id="login_form"')):
                reason = 'login_required'
        status = reason if reason in STOP else 'ok' if rows else reason or 'no_public_posts'
        result = {'rows':rows,'status':status}
        if diagnostics is not None: result['parser_diagnostics'] = diagnostics
        if reason: result['stop_reason'] = reason
    except StopCollection as error:
        result = {'rows':[],'status':error.status}
    except subprocess.TimeoutExpired:
        result = {'rows':[],'status':'timeout'}
    except Exception as error:
        result = {'rows':[],'status':'extraction_failed','error_type':type(error).__name__}
    output.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')


if __name__ == '__main__':
    main()
