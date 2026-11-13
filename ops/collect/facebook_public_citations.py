"""Find Facebook permalinks cited in a bounded public news topic archive."""
import json
from pathlib import Path
import re
import time
from urllib.parse import urljoin,urlsplit
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup
from cloud_social import require_cloud
from facebook_older import candidate

ROOT='https://suspilne.media'
TOPIC=ROOT+'/tag/dsns/'
REFERENCES=[
    ROOT+'/kherson/1388218-povtornij-udar-pisla-pributta-ratuvalnikiv-rosijski-okupanti-atakuvali-pozeznij-avtomobil-u-hersoni/',
    ROOT+'/kherson/1365560-u-hersoni-gorili-avto-aki-atakuvav-rosijskij-fpv-dron/',
]


def collect():
    require_cloud()
    links={}; articles=list(REFERENCES); indexes=[TOPIC]; blocked=False
    session=requests.Session();session.trust_env=False
    session.headers['User-Agent']='ArgusPublicCollector/1.0'
    policy=RobotFileParser()
    def read(url,robots=False):
        nonlocal blocked
        if blocked or not url.startswith(ROOT+'/') or (not robots and not policy.can_fetch('ArgusPublicCollector',url)):
            return ''
        try:
            with session.get(url,timeout=(3,5),allow_redirects=False,stream=True) as r:
                if r.status_code in (401,403,429):blocked=True
                if r.status_code!=200:
                    if robots and r.status_code!=404:blocked=True
                    return ''
                data=r.raw.read(2*1024**2+1,decode_content=True)
                return data.decode('utf-8',errors='replace') if len(data)<=2*1024**2 else ''
        except requests.RequestException:
            if robots:blocked=True
            return ''
    try:
        policy.parse(read(ROOT+'/robots.txt',True).splitlines())
        end=time.monotonic()+100
        for url in indexes:
            if blocked or time.monotonic()>end:break
            doc=BeautifulSoup(read(url),'html.parser')
            for a in doc.select('a[href]'):
                link=urljoin(url,a['href'])
                if urlsplit(link).netloc!='suspilne.media':continue
                if re.fullmatch(r'/(?:[a-z-]+/)?\d+-[^/]+/',urlsplit(link).path) and link not in articles:
                    articles.append(link)
                if url==TOPIC and a.get_text(strip=True) in ('2','3','4') and link.startswith(TOPIC) and link not in indexes:
                    indexes.append(link)
            time.sleep(1)
        checked=0
        for url in articles[:42]:
            if blocked or time.monotonic()>end:break
            doc=BeautifulSoup(read(url),'html.parser');checked+=1
            for a in doc.select('a[href]'):
                found=candidate(a['href'])
                if found:links[found['url']]=url
            time.sleep(1)
        print(json.dumps({'articles_checked':checked,'public_facebook_citations':len(links),'blocked':blocked}))
    finally:
        session.close()
    Path('public-citations.json').write_text(json.dumps(links),encoding='utf-8')


if __name__=='__main__':collect()
