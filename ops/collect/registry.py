"""Read a private JSONBin registry once; never execute remote commands or URLs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request

ENGINES={'telegram':'telegram_public_html','youtube':'youtube_rss','rutube':'rutube_jsonld',
         'rss':'feedparser_jsonld','mailru':'feedparser_jsonld','facebook':'playwright',
         'instagram':'playwright','x':'playwright','tiktok':'playwright','threads':'playwright',
         'vk':'playwright','ok':'playwright','max':'unsupported','reddit':'reddit_rss','whatsapp':'unsupported'}
EXTENSIONS={'gdelt':'gdelt_public_json'}
OPTIONAL={'x':'twscrape','instagram':'instagrapi'}
MAX_BYTES=60000

def default_registry():
    return {'schema':'argus-collector-registry-v1','execution':{'runner':'github-hosted','max_posts':5,'media_fallback':True},
        'storage':'google_drive','collectors':[{'platform':p,'engine':engine,'enabled':True,
            **({'optional_adapter':{'name':OPTIONAL[p],'enabled':False,'state':'not_installed_requires_account_session'}} if p in OPTIONAL else {})}
            for p,engine in ENGINES.items()]}

def validate(value):
    if not isinstance(value,dict) or set(value)!={'schema','execution','storage','collectors'}:raise ValueError('Invalid registry fields')
    if value['schema']!='argus-collector-registry-v1' or value['storage']!='google_drive':raise ValueError('Invalid registry format')
    config=value['execution']
    if not isinstance(config,dict) or set(config)!={'runner','max_posts','media_fallback'}:raise ValueError('Invalid execution settings')
    if config['runner']!='github-hosted' or type(config['max_posts']) is not int or not 1<=config['max_posts']<=5 or type(config['media_fallback']) is not bool:
        raise ValueError('Invalid execution bounds')
    if not isinstance(value['collectors'],list):raise ValueError('Invalid collectors')
    seen=set()
    for item in value['collectors']:
        if not isinstance(item,dict):raise ValueError('Invalid collector')
        p=item.get('platform')
        expected={'platform','engine','enabled'}|({'optional_adapter'} if p in OPTIONAL else set())
        if set(item)!=expected or p not in ENGINES|EXTENSIONS or p in seen or item['engine']!=(ENGINES|EXTENSIONS)[p] or type(item['enabled']) is not bool:
            raise ValueError('Unsupported collector configuration')
        if p in OPTIONAL and item['optional_adapter']!={'name':OPTIONAL[p],'enabled':False,'state':'not_installed_requires_account_session'}:
            raise ValueError('Authenticated adapter is not installed; configuration cannot grant authorization')
        seen.add(p)
    if not set(ENGINES)<=seen:raise ValueError('Missing collector configuration')
    return value

def encode(value):return json.dumps(validate(value),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise ValueError('Registry redirect rejected')

def fetch_registry(bin_id,key,key_type='access'):
    if not re.fullmatch(r'[a-f0-9]{24}',bin_id) or not key or key_type not in ('access','master'):raise ValueError('Invalid registry credentials')
    header='X-Access-Key' if key_type=='access' else 'X-Master-Key'
    req=urllib.request.Request('https://api.jsonbin.io/v3/b/'+bin_id+'/latest',headers={header:key,'X-Bin-Meta':'true','User-Agent':'Argus-registry-setup','Accept':'application/json'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req,timeout=25) as response:raw=response.read(MAX_BYTES+1)
    except urllib.error.HTTPError as error:raise RuntimeError('JSONBin HTTP '+str(error.code)) from None
    except urllib.error.URLError:raise RuntimeError('JSONBin connection failed') from None
    if len(raw)>MAX_BYTES:raise ValueError('Registry response too large')
    envelope=json.loads(raw)
    if envelope.get('metadata',{}).get('private') is not True:raise ValueError('Registry must be private')
    return validate(envelope['record'])

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted':raise RuntimeError('Registry fetch requires GitHub-hosted Actions')
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);args=p.parse_args()
    value=fetch_registry(os.environ.get('ARGUS_JSONBIN_BIN_ID',''),os.environ.get('ARGUS_JSONBIN_KEY',''),os.environ.get('ARGUS_JSONBIN_KEY_TYPE','access'))
    payload=encode(value);path=Path(args.output);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
    print(json.dumps({'private_registry':True,'collectors':len(value['collectors']),'sha256':hashlib.sha256(payload).hexdigest()}))

if __name__=='__main__':main()
