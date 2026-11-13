"""Official MAX SDK is available only for explicitly mapped bot-admin channels."""
from private_assets import private_asset_path
import datetime as dt
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from cloud_social import require_cloud,process,tool_root,MANIFEST,platform_token


def normalize(source,rows,chat_id):
    from run import post,url_ok,HOSTS
    items={}
    for row in rows[:100]:
        recipient=row.get('recipient') or {}; body=row.get('body') or {}; mid=body.get('mid',''); url=row.get('url','')
        if recipient.get('chat_id')!=chat_id or recipient.get('chat_type')!='channel': continue
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',mid) or not url_ok(url) or urlsplit(url).hostname not in HOSTS['max']:continue
        media=[]
        for attachment in body.get('attachments') or []:
            payload=attachment.get('payload') or {}
            if isinstance(payload,dict) and url_ok(payload.get('url')): media.append(payload['url'])
        text=body.get('text') or ''
        if not text and not media:continue
        stamp=row.get('timestamp'); published=dt.datetime.fromtimestamp(stamp/1000,dt.timezone.utc).isoformat() if isinstance(stamp,int) and stamp>0 else None
        key='max:'+str(chat_id)+'/'+mid
        items[key]=post(source,key,url,text,published,media)
        items[key].update(capture_mode='max_sdk',collector_commit=MANIFEST['max-sdk']['commit'])
    return list(items.values())


async def collect(source,limit):
    require_cloud()
    configs=json.loads(private_asset_path('max-channels.json').read_text())['channels']
    config=next((c for c in configs if c.get('url','').rstrip('/')==source['url'].rstrip('/')),None)
    status='requires_max_channel_mapping' if not config else 'requires_max_bot_token' if not platform_token('ARGUS_MAX_BOT_TOKEN') else None
    items=[]
    if status is None:
        chat_id=config.get('chat_id')
        if type(chat_id) is not int or chat_id==0:raise ValueError('invalid_max_chat_id')
        with tempfile.TemporaryDirectory(prefix='argus-max-') as directory:
            root=Path(directory); request=root/'request.json'; output=root/'result.json'
            request.write_text(json.dumps({'chat_id':chat_id,'limit':min(limit,100)}))
            code,status,_=await process([str(tool_root()/'max-read'),str(request),str(output)],root,output,timeout=20,
                extra_env={'ARGUS_MAX_BOT_TOKEN':platform_token('ARGUS_MAX_BOT_TOKEN')})
            if not status and not code:
                data=json.loads(output.read_text()); items=normalize(source,data.get('rows') or [],chat_id); status=data['status']
                if status=='ok':status='ok' if items else 'no_verified_posts'
            else:status=status or 'worker_failed'
    return items,status,[{'tool':'max-sdk','status':status,'posts':len(items),'upstream_commit':MANIFEST['max-sdk']['commit']}]
