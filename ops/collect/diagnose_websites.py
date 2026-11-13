"""Read-only bounded diagnostic of the same 20 public catalog sources."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE=Path(__file__).resolve().parent
from run import safe_url,Redirects
from bs4 import BeautifulSoup

def check(url,out):
    record={'url':url};start=time.monotonic()
    try:
        safe_url(url)
        req=urllib.request.Request(url,headers={'User-Agent':'ArgusPRO/1.0 public-feed-reader','Accept':'text/html,application/rss+xml,application/atom+xml'})
        try:response=urllib.request.build_opener(Redirects()).open(req,timeout=12)
        except urllib.error.HTTPError as error:response=error
        with response:
            body=response.read(2*1024**2)
            record.update(status=response.code,final_url=response.url,
                headers={k:response.headers.get(k) for k in ('Content-Type','Server','Via','Retry-After','CF-Ray','X-Cache') if response.headers.get(k)},
                bytes=len(body))
        soup=BeautifulSoup(body,'html.parser')
        record['title']=soup.title.get_text(' ',strip=True)[:200] if soup.title else None
        if record['status']>=400:record['error_excerpt']=soup.get_text(' ',strip=True)[:800]
        record['feeds']=[dict(n.attrs) for n in soup.select('link[rel="alternate"][href]')[:8]]
        record['h1']=[n.get_text(' ',strip=True)[:200] for n in soup.select('h1')[:3]]
        record['metadata']=[dict(n.attrs) for n in soup.select('meta[property],meta[name],link[rel="canonical"]')[:60]]
        path=out/(hashlib.sha256(url.encode()).hexdigest()[:16]+'.html');path.write_bytes(body);record['file']=str(path)
    except Exception as error:
        reason=getattr(error,'reason',error)
        record.update(error=type(error).__name__,reason_type=type(reason).__name__,reason=str(reason)[:300])
    record['seconds']=round(time.monotonic()-start,2)
    return record

def main():
    out=Path('collection/catalog-diagnostics');out.mkdir(parents=True,exist_ok=True)
    sources=json.loads((BASE/'sources.json').read_text(encoding='utf-8'))['sources']
    selected=[s for s in sources if s['platform']=='rss' and s.get('enabled',True)][:20]
    urls=list(dict.fromkeys(u for s in selected for u in (s['url'],urllib.parse.urljoin(s['url'],'/robots.txt'))))
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda u:check(u,out),urls))
    (out/'report.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{k:r[k] for k in ('url','status','title','error_excerpt','error','reason_type','reason','seconds') if k in r} for r in results],ensure_ascii=True))

if __name__=='__main__':main()
