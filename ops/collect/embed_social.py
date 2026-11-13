"""New anonymous engines, preceding legacy login-dependent fallbacks."""
import asyncio
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from cloud_social import require_cloud, process, tool_root, STOP
from remaining_social import normalize

MANIFEST = json.loads(Path(__file__).with_name('embed-upstreams.json').read_text())
CHAINS = {'instagram':('instagramprofiles','gallery-dl'), 'facebook':('rss-bridge','gallery-dl')}


def diagnostic(tool,status,items):
    result = {'tool':tool,'status':status,'posts':len(items),'upstream_commit':MANIFEST[tool]['commit']}
    if tool=='gallery-dl':
        result['supporting_tools'] = {'gallery-dl-web':MANIFEST['gallery-dl-web']['commit']}
    return result


async def adapter(source,limit,tool):
    require_cloud()
    from run import source_valid
    if not source_valid(source) or tool not in CHAINS.get(source['platform'],()):
        return [],diagnostic(tool,'invalid_source',[])
    if not 1 <= limit <= 100:
        raise ValueError('invalid_limit')
    if tool=='instagramprofiles' and not re.fullmatch(r'/[A-Za-z0-9_.]{1,30}/?',urlsplit(source['url']).path):
        return [],diagnostic(tool,'unsupported_source_shape',[])
    if tool=='rss-bridge' and urlsplit(source['url']).path.startswith('/groups/'):
        return [],diagnostic(tool,'unsupported_source_shape',[])
    with tempfile.TemporaryDirectory(prefix='argus-embed-') as directory:
        work = Path(directory)
        request, output = work/'request.json', work/'result.json'
        request.write_text(json.dumps({'source':source,'tool':tool,'limit':limit,'tools':str(tool_root())}),encoding='utf-8')
        command = [str(tool_root()/'embed-python/bin/python'),str(Path(__file__).with_name('embed_worker.py')),str(request),str(output)]
        code,status,_ = await process(command,work,output,timeout=45)
        if code or status or not output.exists():
            return [],diagnostic(tool,status or 'worker_failed',[])
        data = json.loads(output.read_text(encoding='utf-8'))
        items = normalize(source,data.get('rows',[]),limit,tool,MANIFEST[tool]['commit'])
        status = data.get('status','no_public_posts')
        if status not in STOP:
            status = 'ok' if items else 'unverified_posts' if data.get('rows') else status
        if tool=='instagramprofiles':
            for item in items: item['media_are_previews'] = True
        attempt = diagnostic(tool,status,items)
        details = data.get('parser_diagnostics',{})
        if isinstance(details,dict):
            allowed = ('response_bytes','context_marker','context_null','profile_parsed','profile_matches','parsed_posts')
            safe = {key:value for key,value in details.items() if key in allowed and type(value) in (bool,int)}
            if safe: attempt['parser_diagnostics'] = safe
        for field in ('error_type','stop_reason'):
            if re.fullmatch(r'[A-Za-z0-9_]{1,80}',str(data.get(field,''))):
                attempt[field] = data[field]
        return items,attempt


async def collect(source,limit):
    require_cloud()
    attempts = []
    for tool in CHAINS[source['platform']]:
        try:
            items,attempt = await adapter(source,limit,tool)
        except Exception:
            items,attempt = [],diagnostic(tool,'adapter_failed',[])
        attempts.append(attempt)
        if items or attempt['status'] in STOP:
            return items,attempts
        await asyncio.sleep(1)
    return [],attempts
