"""Import contracts must not borrow dependencies from a different interpreter."""
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from collection_diagnostics import diagnostic, import_failure_code
from collector_import_smoke import LANES, smoke


class CollectorImportTests(unittest.TestCase):
    def test_missing_dependency_differs_from_internal_module_and_symbol_errors(self):
        cases = ((ModuleNotFoundError('not exported', name='requests'), 'dependency_missing'),
                 (ModuleNotFoundError('not exported', name='source_adapters'), 'internal_module_missing'),
                 (ImportError('not exported', name='cloud_social'), 'internal_import_error'))
        from run import error_code
        for error, code in cases:
            self.assertEqual(import_failure_code(error), code)
            self.assertEqual(error_code(error), code)
            self.assertNotIn('not exported', json.dumps(diagnostic(error, code)))
        error = ModuleNotFoundError('private message', name='private_identifier')
        self.assertEqual(diagnostic(error, 'dependency_missing')['import_module'], 'NOT_VERIFIED')

    def test_real_circular_import_is_not_reported_as_missing_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'smoke_cycle_a.py').write_text('from smoke_cycle_b import value\nvalue = 1\n')
            (root / 'smoke_cycle_b.py').write_text('from smoke_cycle_a import value\nvalue = 2\n')
            with patch.object(sys, 'path', [directory, *sys.path]):
                try:
                    importlib.import_module('smoke_cycle_a')
                except ImportError as error:
                    self.assertEqual(import_failure_code(error), 'circular_import_error')
                    row = diagnostic(error, import_failure_code(error))
                    self.assertNotIn(directory, json.dumps(row))
                else:
                    self.fail('Expected an actual circular import')
                finally:
                    sys.modules.pop('smoke_cycle_a', None)
                    sys.modules.pop('smoke_cycle_b', None)

    def test_main_manifest_declares_direct_http_imports(self):
        manifest = Path(__file__).with_name('requirements.txt').read_text().splitlines()
        self.assertIn('requests==2.34.2', manifest)
        self.assertIn('urllib3==2.8.0', manifest)

    def test_missing_dependency_fails_smoke_with_safe_module_identity(self):
        def importer(name):
            if name == 'requests':
                raise ModuleNotFoundError('private message', name='requests')
        result = smoke('fast', importer=importer)
        self.assertFalse(result['passed'])
        self.assertEqual(result['imports'][0]['import_module'], 'requests')
        self.assertNotIn('private message', json.dumps(result))

    def test_real_main_imports_in_both_lane_orders_in_fresh_processes(self):
        for lane in LANES:
            for reverse in (False, True):
                command = [sys.executable, str(Path(__file__).with_name('collector_import_smoke.py')), '--lane', lane]
                if reverse:
                    command.append('--reverse')
                process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
                self.assertEqual(process.returncode, 0, 'Import smoke failed; inspect sanitized stdout')
                result = json.loads(process.stdout)
                self.assertTrue(result['passed'])
                self.assertEqual(len(result['imports']), len(LANES[lane]))

    def test_shared_access_policy_remains_identical(self):
        from cloud_social import STOP, WALL
        from source_adapters import WALL as adapter_wall
        self.assertIs(WALL, adapter_wall)
        self.assertEqual(WALL, STOP | {'unexpected_destination', 'invalid_source', 'deferred_rate_limit', 'disabled_by_registry'})

    def test_live_and_pr_workflows_smoke_the_actual_lane_interpreters(self):
        root = Path(__file__).resolve().parents[2]
        live = (root / '.github/workflows/argus-collection-lane.yml').read_text()
        ci = (root / '.github/workflows/public-validation.yml').read_text()
        self.assertLess(live.index('Smoke main collector interpreter'), live.index('Validate explicit public source catalog'))
        self.assertIn('.social-tools/embed-python/bin/python ops/collect/collector_import_smoke.py', live)
        self.assertIn('.social-tools/remaining-python/bin/python ops/collect/collector_import_smoke.py', live)
        job = ci.split('  collector-imports:', 1)[1].split('  frontend:', 1)[0]
        self.assertNotIn('secrets.', job)
        self.assertNotIn('pull_request_target', ci)
        self.assertIn('lane: [fast, extended]', job)
        dockerfile = (root / 'ops/hostless/Dockerfile').read_text()
        self.assertIn('COPY .github/workflows/public-validation.yml /app/.github/workflows/public-validation.yml', dockerfile)

    def test_adapter_first_import_order_has_no_internal_cycle(self):
        process = subprocess.run([sys.executable, '-c',
            'import source_adapters; import run; from cloud_social import WALL; assert source_adapters.WALL is WALL'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertEqual(process.returncode, 0, 'Adapter-first import contract failed')

    def test_successful_extraction_reaches_reporting_without_optional_requests(self):
        # Fresh interpreter: cached source_adapters/requests in the test runner
        # must not hide the baseline's unconditional optional-adapter import.
        code = r'''
import asyncio, contextlib, importlib.abc, io, json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
class MissingRequests(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] == 'requests':
            raise ModuleNotFoundError('offline fixture dependency absent', name='requests')
sys.meta_path.insert(0, MissingRequests())
import run
async def inline(function, *args, **kwargs): return function(*args, **kwargs)
async def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        platforms = ('rss', 'mailru', 'youtube', 'telegram', 'threads', 'instagram', 'ok', 'rutube')
        sources = [{'id': 'fixture-'+p, 'platform': p, 'url': 'https://example.org/'+p} for p in platforms]
        (root/'sources.json').write_text(json.dumps({'sources': sources}))
        args = SimpleNamespace(sources=root/'sources.json', output=root/'out', registry=None, cursors=None,
            incremental=True, bounded_poll=True, adaptive=False, telegram_history=True, sources_per_platform=0,
            posts=5, shard_index=0, shard_count=1, media_fallback=False, concurrency=2,
            source_timeout=30, batch_seconds=180)
        def rows(source):
            return [run.post(source, source['platform']+':fixture/1', 'https://example.org/post/'+source['platform'],
                             'Fixture publication', run.now())]
        def native(source, *args, **kwargs):
            return rows(source), 'ok', [{'tool':'public_feed_history','status':'ok','posts':1}], None
        async def reader(browser, source, *args, **kwargs):
            return native(source)[:3]
        async def metrics(source, items, previous, **kwargs): return items, None
        runtime = MagicMock()
        with patch('runtime_policy.require_cloud'), patch.object(run, 'source_valid', return_value=True), \
             patch('playwright.async_api.async_playwright', return_value=runtime), patch('asyncio.to_thread', inline), \
             patch('feed_history.collect', side_effect=native), patch('youtube_history.collect', side_effect=native), \
             patch('telegram_history.collect', side_effect=native), patch.object(run, 'collect_source', side_effect=reader), \
             patch('post_metrics.bounded_refresh', side_effect=metrics), \
             patch.dict(os.environ, {'ARGUS_SOURCE_ADAPTERS':'0','ARGUS_FACEBOOK_X':'0','ARGUS_FACEBOOK_BROWSER_ONLY':'true'}), \
             contextlib.redirect_stdout(io.StringIO()):
            await run.run(args)
        report = json.loads((root/'out/report.json').read_text())
        posts = [json.loads(line) for line in (root/'out/posts.jsonl').read_text().splitlines()]
        print(json.dumps({'outcomes': {r['platform']:r['status'] for r in report['sources']},
                          'posts':len(posts), 'dated':sum(bool(p.get('published_at')) for p in posts),
                          'adapter_imported':'source_adapters' in sys.modules}))
asyncio.run(main())
'''
        process = subprocess.run([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(process.returncode, 0, 'Offline pipeline fixture failed')
        result = json.loads(process.stdout)
        self.assertEqual(len(result['outcomes']), 8)
        self.assertEqual(set(result['outcomes'].values()), {'ok'})
        self.assertEqual((result['posts'], result['dated']), (8, 8))
        self.assertFalse(result['adapter_imported'])
