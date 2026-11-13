"""Budgets for anonymous readers. A target never overrides a server's stop or throttle."""
import json
from contextvars import ContextVar
from pathlib import Path

CONFIG = json.loads(Path(__file__).with_name('depth.json').read_bytes())
mode = ContextVar('depth_mode', default='live')
browser_progress = ContextVar('browser_progress',default=None)
bounded_poll = ContextVar('bounded_poll',default=False)
attempt_trace = ContextVar('attempt_trace',default=None)
source_deadline = ContextVar('source_deadline',default=None)


def policy(platform):
    lane = mode.get()
    config = CONFIG[lane]
    pages = CONFIG['platforms'].get(platform, {}).get(lane+'_pages', config['target_max_pages_per_source'])
    if lane=='live' and bounded_poll.get():
        pages=min(pages,{'telegram':3,'rss':5,'mailru':5,'browser':2}.get(platform,3))
        return pages,config['target_max_posts_per_source'],25
    return min(pages,config['target_max_pages_per_source']),config['target_max_posts_per_source'],config['source_seconds']
