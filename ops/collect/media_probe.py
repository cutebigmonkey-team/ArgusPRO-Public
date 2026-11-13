"""Test real catalog sources; keep diagnostics separate from the main archive."""
import asyncio,json,os
from pathlib import Path
from media_metadata import extract

async def main():
    catalog=json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8'))
    selected=[]
    for platform in ('youtube','rutube'):
        selected += [s for s in catalog['sources'] if s['platform']==platform and s.get('enabled',True)][:2]
    results=[];posts={}
    async def one(source):
        items,report=await extract(source,2)
        report.update(source_id=source['id'],platform=source['platform'],url=source['url'],mode='channel_or_video')
        results.append(report)
        for item in items:posts[item['canonical_key']]=item
        if items and source['platform']=='youtube':
            direct={**source,'url':items[0]['post_url']}
            details,detail_report=await extract(direct,1,flat=False)
            detail_report.update(source_id=source['id'],platform=source['platform'],url=direct['url'],mode='direct_video_details')
            results.append(detail_report)
            for item in details:posts[item['canonical_key']]=item
    # Two simultaneous sources; no account credentials or proxy.
    for index in range(0,len(selected),2):await asyncio.gather(*(one(s) for s in selected[index:index+2]))
    out=Path('collection/media-probe');out.mkdir(parents=True,exist_ok=True)
    value={'run_id':os.environ['GITHUB_RUN_ID'],'version':'2026.10.26','sources':results,'unique_posts':len(posts),
           'media_mode':'metadata_only','cookies':False,'proxy':False}
    (out/'report.json').write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'posts.json').write_text(json.dumps(list(posts.values()),ensure_ascii=False),encoding='utf-8')
    print(json.dumps(value))
if __name__=='__main__':asyncio.run(main())
