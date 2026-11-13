"""Bounded TypeSafe slots inside the existing single archive writer.

No extra service, no GitHub dispatch, no second writer and no collector access
to the API key. The existing ledger owns budgets, deduplication and retries.
"""
import asyncio
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


@lru_cache(maxsize=1)
def worker_class():
    directory = Path(__file__).resolve().parents[1] / 'ai'
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(directory))
        spec = importlib.util.spec_from_file_location('argus_typesafe_worker', directory / 'worker.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.Worker, module.Client
    finally:
        sys.path[:] = previous


class TypeSafeSchedule:
    def __init__(self, emit, clock=time.monotonic):
        self.emit, self.clock = emit, clock
        self.next_run = 0
        self.settings = {}

    def run_due(self, store, feed, journal):
        now = self.clock()
        if now < self.next_run:
            return
        self.next_run = now + 300
        if not self.settings.get('enabled', os.getenv('ARGUS_TYPESAFE_ENABLED') == 'true'):
            return
        if os.getenv('ARGUS_ARCHIVE_WRITER_OWNER') != 'hostless':
            raise RuntimeError('typesafe_writer_not_owned')
        if not os.getenv('TYPESAFE_API_KEY'):
            self.emit('typesafe_unavailable', reason='missing_api_key')
            return
        journal.check_session()
        try:
            feed.verify_head()
            config = json.loads(os.environ['ARGUS_TYPESAFE_CONFIG'])
            # Give collection its next slot promptly. Never raise existing limits.
            config['max_posts'] = min(4, int(config.get('max_posts', 50)))
            config['concurrency'] = min(2, int(config.get('concurrency', 2)))
            for key in ('concurrency','max_posts'):
                if key in self.settings:config[key]=min(config[key],self.settings[key])
            if 'model' in self.settings:config['model']=self.settings['model']
            Worker, Client = worker_class()
            self.emit('typesafe_started', sequence=(feed.head or {}).get('last', 0))
            result = asyncio.run(Worker(store, Client(), config, guard=journal.check_session).run(
                feed.snapshot, store.read_json('typesafe-policy.json') or {}, max_seconds=60, request_seconds=30))
            self.emit('typesafe_published', sequence=(feed.head or {}).get('last', 0), **result)
        except Exception as error:
            # A lost writer lock must stop the whole writer; a derived-product
            # failure can wait until the next slot without replaying uncertain HTTP.
            journal.check_session()
            self.emit('typesafe_failed', error_type=type(error).__name__)
        finally:
            self.next_run = self.clock() + self.settings.get('interval',300)
