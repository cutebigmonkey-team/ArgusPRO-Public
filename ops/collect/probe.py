"""Small real-source probe on GitHub; never publishes to the archive."""
import argparse
import asyncio
import json
from pathlib import Path
import run


def main():
    catalog=json.loads(Path('ops/collect/sources.json').read_text(encoding='utf-8'))
    selected=[]
    for platform in ('telegram','youtube','x','facebook','instagram','rss','tiktok','threads','vk','whatsapp'):
        candidates=[s for s in catalog['sources'] if s['platform']==platform and run.source_valid(s)]
        if platform=='rss':candidates.sort(key=lambda s:('dsns.gov.ua' not in s['url'],s['url']))
        if platform=='instagram':candidates.sort(key=lambda s:(s['url']!='https://www.instagram.com/fixture584',s['url']))
        if platform=='tiktok':candidates=[s for s in candidates if '/@' in s['url']]
        selected.extend(candidates[:2])
    root=Path('collection/probe');root.mkdir(parents=True,exist_ok=True)
    source_file=root/'input.json';source_file.write_text(json.dumps({**catalog,'sources':selected}),encoding='utf-8')
    args=argparse.Namespace(sources=str(source_file),output=str(root),sources_per_platform=0,posts=3,
                            shard_index=0,shard_count=1,media_fallback=True,registry=None)
    asyncio.run(run.run(args))


if __name__=='__main__':main()
