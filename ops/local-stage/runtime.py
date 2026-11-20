"""Run the existing continuous collector and journal on disposable loopback PostgreSQL."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

OPS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(OPS / name) for name in ('hostless', 'collect', 'drive', 'local-storage')]
from continuous import Child, FencedStore, Limits, Loop
from drive_store import encoded, MAX_JSON
from indexed_journal import IndexedFeedJournal
from publish import load_snapshot
from registry import default_registry
from schedule_store import ScheduleStore
from serialized_db import Connection
from worker import Pending, assemble, collector_environment, emit


class LocalArchive:
    """Atomic filesystem objects implementing the existing archive contract."""

    def __init__(self, root):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def assert_private(self):
        if self.root.stat().st_mode & 0o077:
            raise ValueError('archive_directory_not_private')

    def path(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}', name) or name in ('.', '..'):
            raise ValueError('invalid_archive_object')
        return self.root / name

    def read_bytes(self, name, limit):
        try:
            with self.path(name).open('rb') as reader:
                raw = reader.read(limit + 1)
        except FileNotFoundError:
            return None
        if len(raw) > limit:
            raise ValueError('archive_object_too_large')
        return raw

    def read_json(self, name):
        raw = self.read_bytes(name, MAX_JSON)
        return json.loads(raw) if raw is not None else None

    def find(self, name):
        raw = self.read_bytes(name, MAX_JSON)
        if raw is not None:
            return {'id': self.identity(name), 'size': str(len(raw)),
                    'md5Checksum': hashlib.md5(raw, usedforsecurity=False).hexdigest()}
        return None

    @staticmethod
    def identity(name):
        return hashlib.sha256(name.encode()).hexdigest()

    def put(self, name, data, mutable=False):
        target = self.path(name)
        previous = self.read_bytes(name, max(MAX_JSON, len(data)))
        if previous is not None and not mutable:
            if previous != data:
                raise ValueError('immutable_archive_object')
            return self.identity(name)
        with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as writer:
            temporary = Path(writer.name)
            try:
                writer.write(data)
                writer.flush()
                os.fsync(writer.fileno())
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return self.identity(name)

    def json(self, name, data, mutable=False):
        return self.put(name, encoded(data), mutable)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--pg-port', type=int, default=55472)
    args = parser.parse_args()
    import psycopg
    from psycopg.rows import dict_row
    from comments_store import Comments
    from check_metrics import summarize

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())
    catalog = json.loads(args.sources.read_text())
    registry = default_registry()
    enabled = {row['platform'] for row in registry['collectors'] if row['enabled']}
    if any(source['platform'] not in enabled for source in catalog['sources']):
        raise ValueError('unsupported_stage_platform')
    archive = LocalArchive(args.state.resolve() / 'archive')
    archive.assert_private()
    with psycopg.connect(host='127.0.0.1', port=args.pg_port, dbname='argus_local_stage',
                         user='argus_local', sslmode='disable', connect_timeout=3,
                         autocommit=True, row_factory=dict_row) as db:
        if not db.execute('SELECT pg_try_advisory_lock(18765) AS owned').fetchone()['owned']:
            raise SystemExit('local stage already has a writer')
        def guard():
            db.execute('SELECT 1')
        lock = threading.RLock()
        fenced = FencedStore(archive, guard, lock)
        fenced.json('sources.json', catalog, mutable=True)
        connection = Connection(db, lock)
        schedule = ScheduleStore(connection)
        feed = IndexedFeedJournal(fenced)
        journal = SimpleNamespace(db=SimpleNamespace(connection=connection), check_session=guard)
        pending = Pending(journal, schedule, feed)
        comments = Comments(connection)
        try:
            def export():
                fenced.json('local-snapshot.json', load_snapshot(fenced), mutable=True)
            if pending.prepare():
                pending.recover(fenced)
            schedule.release_abandoned()
            schedule.sync(catalog, feed.verified_state, enabled)
            export()
            emit('local_stage_ready', sources=len(catalog['sources']))

            def stage(limit):
                with tempfile.TemporaryDirectory() as path:
                    batch = assemble(schedule, Path(path), max_bytes=limit)
                    if batch:
                        pending.stage(*batch)

            report = {'run_id': 'local-startup', 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                      'sources': [], 'new_to_archive': 0, 'incremental': True}
            def delivered():
                nonlocal report
                report = loop.item['report']
                export()

            def maintain():
                feed.maintenance(report)
                comments.sync(feed.index.rows(), catalog['sources'])
                comments.tick(emit)
                comments.publish(fenced)

            loop = Loop(schedule=schedule, pending=pending,
                        start=lambda sources: Child(sources, feed.verified_state, registry,
                                                    collector_environment, OPS, emit=emit),
                        deliver=lambda item: pending.publish(fenced, item), assemble=stage,
                        guard=guard, emit=emit, maintenance=maintain, on_delivery=delivered,
                        sql_lock=lock,
                        limits=Limits(fast_sources=2, other_sources=2, maintenance_seconds=30))
            try:
                next_sample = 0
                while True:
                    if stopping.is_set():
                        loop.stop()
                    if not loop.tick():
                        break
                    if time.monotonic() >= next_sample:
                        now = time.time()
                        windows = summarize(schedule.execute, now)
                        sources = connection.execute('SELECT id, enabled, health, next_check_at, last_attempt_at FROM runtime_schedule ORDER BY id').fetchall()
                        for source in sources:
                            source['health'] = json.loads(source['health'])
                        fenced.json('local-health.json', {'at': now, 'sources': sources,
                                    'queues': schedule.metrics(now), 'windows': windows}, mutable=True)
                        next_sample = time.monotonic() + 10
                    stopping.wait(.2) if not stopping.is_set() else time.sleep(.2)
            finally:
                loop.close()
        finally:
            comments.close()
            feed.close()


if __name__ == '__main__':
    main()
