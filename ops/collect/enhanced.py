"""Bounded subprocess adapters; isolated event loops and a hard lifetime limit."""
from private_assets import private_asset_path
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

STOP={'http_401','http_403','http_429','challenge_required','login_required','robots_denied'}


def config(path=None):
    value=json.loads(Path(path or private_asset_path('enhanced-config.json')).read_text(encoding='utf-8'))
    if (set(value)!={'schema','default_engine','source_engines','max_pages','rsshub_base'}
            or value['schema']!='argus-enhanced-collection-v1'
            or value['default_engine'] not in ('crawlee','scrapy')
            or type(value['max_pages']) is not int or not 1<=value['max_pages']<=4
            or not isinstance(value['source_engines'],dict)
            or any(v not in ('crawlee','scrapy') for v in value['source_engines'].values())):
        raise ValueError('invalid_enhanced_config')
    from run import url_ok
    if not url_ok(value['rsshub_base']) or urlsplit(value['rsshub_base']).scheme!='https':
        raise ValueError('invalid_rsshub_base')
    return value


async def collect(source,limit,settings):
    engine=settings['source_engines'].get(str(source.get('id','')),settings['default_engine'])
    with tempfile.TemporaryDirectory(prefix='argus-crawler-') as temp:
        root=Path(temp);request=root/'request.json';output=root/'result.json'
        request.write_text(json.dumps({'source':source,'limit':min(limit,100),'max_pages':settings['max_pages']}),encoding='utf-8')
        from cloud_social import worker_env
        env={**worker_env(),'CRAWLEE_STORAGE_DIR':str(root/'storage'),'CRAWLEE_PURGE_ON_START':'true',
             'CRAWL4_AI_BASE_DIRECTORY':str(root),'ARGUS_ENHANCED_COLLECTION':'1','LITELLM_LOCAL_MODEL_COST_MAP':'True'}
        with (root/'worker.log').open('wb') as log:
            process=await asyncio.create_subprocess_exec(sys.executable,str(Path(__file__).with_name('crawl_worker.py')),
                engine,str(request),str(output),stdout=log,stderr=log,env=env)
            try:await asyncio.wait_for(process.wait(),timeout=85)
            except (asyncio.TimeoutError,asyncio.CancelledError):
                if process.returncode is None:process.kill()
                await process.wait()
                raise
        if process.returncode or not output.exists():raise ValueError('crawler_worker_failed')
        result=json.loads(output.read_text(encoding='utf-8'))
        return result['posts'],{'tool':engine,'status':result['status'],'posts':len(result['posts']),
                                'rejected_rows':result.get('rejected_rows',{}),
                                'raw_rows':result.get('raw_rows',len(result['posts'])),
                                'pages':result['pages'],'errors':result['errors'],'requests':result.get('requests',[])}
