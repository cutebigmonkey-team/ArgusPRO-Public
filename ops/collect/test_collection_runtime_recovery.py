"""Offline integration fixtures exercising the same run() used by the pilot.

Inline thread dispatch keeps these fixtures deterministic in restricted sandboxes;
HTTP, native parsers and real thread integration retain their existing suites.
"""
import asyncio
import json
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import run
from collection_diagnostics import diagnostic, public_status
from pilot_runtime import coverage


async def inline(function, *args, **kwargs):
    return function(*args, **kwargs)


class ErrorPolicyTests(unittest.TestCase):
    def test_transport_and_runtime_exceptions_have_fixed_codes(self):
        for error, code in ((ssl.SSLError('sensitive'), 'tls_error'),
                            (ssl.SSLCertVerificationError('sensitive'), 'tls_certificate_error'),
                            (TypeError('sensitive'), 'runtime_type_error'),
                            (KeyError('sensitive'), 'runtime_key_error'),
                            (ModuleNotFoundError('sensitive'), 'dependency_missing')):
            self.assertEqual(run.error_code(error), code)
            self.assertEqual(public_status(code), code)
            self.assertNotIn('sensitive', json.dumps(diagnostic(error, code)))

    def test_unrecognized_messages_and_diagnostic_fields_cannot_leak(self):
        source = {'id': 'fixture', 'platform': 'rss'}
        report = {'sources': [{'source_id': 'fixture', 'status': 'http_404', 'diagnostics': [
            {'code': 'sensitive', 'exception_type': 'sensitive', 'phase': 'sensitive',
             'module': 'sensitive', 'line': 'sensitive'}]}]}
        result = coverage([source], report, [])
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertEqual(result['platforms']['rss']['failed_checks'], 1)
        self.assertEqual(result['platforms']['rss']['collection_health'], 'BROKEN')

    def test_reader_success_is_not_a_completed_source(self):
        result = coverage([{'id': 'fixture', 'platform': 'threads'}], {'sources': [
            {'source_id': 'fixture', 'status': 'runtime_type_error', 'attempts': [
                {'tool': 'threads_public_json', 'status': 'ok', 'posts': 3}]}]}, [])
        stats = result['platforms']['threads']
        self.assertEqual(stats['completed_checks'], 0)
        self.assertEqual(stats['failed_checks'], 1)
        self.assertEqual(stats['reader_attempts']['threads_public_json'], {'ok': 1})


    def test_http_pool_cannot_start_after_source_deadline(self):
        import depth, http_pool
        token = depth.source_deadline.set(10)
        try:
            with patch('http_pool.time.monotonic', return_value=11), patch.object(http_pool.POOL, 'request') as request:
                with self.assertRaises(TimeoutError):
                    http_pool.request('https://example.org/feed', {}, 12)
                request.assert_not_called()
        finally:
            depth.source_deadline.reset(token)

    def test_http_pool_waits_leave_finalization_headroom(self):
        import depth, http_pool
        token = depth.source_deadline.set(20)
        response = MagicMock(status=200)
        response.read.return_value = b'fixture'
        try:
            with patch('http_pool.time.monotonic', return_value=10), patch.object(http_pool.POOL, 'request', return_value=response) as request:
                http_pool.request('https://example.org/feed', {}, 12)
                self.assertEqual(request.call_args.kwargs['pool_timeout'], 4.5)
                self.assertEqual(request.call_args.kwargs['timeout'].total, 4.5)
                self.assertFalse(request.call_args.kwargs['retries'])
        finally:
            depth.source_deadline.reset(token)


class RuntimeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def execute_fixture(self, platform, *, metrics_error=None, native_error=None):
        import depth
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = {'id': 'fixture', 'platform': platform, 'url': 'https://example.org/feed'}
            (root / 'sources.json').write_text(json.dumps({'sources': [source]}))
            args = SimpleNamespace(sources=root / 'sources.json', output=root / 'output', registry=None,
                cursors=None, incremental=True, adaptive=True, bounded_poll=True, telegram_history=True,
                sources_per_platform=0, posts=5, shard_index=0, shard_count=1, media_fallback=False,
                concurrency=2, source_timeout=30, batch_seconds=180)
            post = run.post(source, platform + ':fixture/1', 'https://example.org/post/1',
                            'Fixture publication', run.now())
            observed = {}
            def native(*a, **kwargs):
                observed.update(kwargs)
                if native_error:raise native_error
                return [post], 'ok', [{'tool': 'public_feed_history', 'status': 'ok', 'posts': 1}], None
            runtime = MagicMock()
            runtime.__aenter__.return_value.chromium.launch = AsyncMock()
            metrics = AsyncMock(side_effect=metrics_error) if metrics_error else AsyncMock(return_value=([post], None))
            with patch('runtime_policy.require_cloud'), patch('playwright.async_api.async_playwright', return_value=runtime), \
                 patch('asyncio.to_thread', inline), patch('feed_history.collect', side_effect=native), \
                 patch('youtube_history.collect', side_effect=native), patch('telegram_history.collect', side_effect=native), \
                 patch('run.source_valid', return_value=True), patch('post_metrics.bounded_refresh', metrics):
                await run.run(args)
            report = json.loads((root / 'output/report.json').read_text())
            posts = [json.loads(x) for x in (root / 'output/posts.jsonl').read_text().splitlines()]
            if native_error:
                self.assertEqual(report['sources'][0]['status'], 'runtime_type_error')
                self.assertEqual(posts, [])
                return report, posts
            self.assertEqual(report['sources'][0]['status'], 'ok')
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]['canonical_key'], post['canonical_key'])
            self.assertLessEqual(observed['seconds'], 25)
            if platform in ('rss', 'mailru'):
                self.assertLessEqual(observed['max_pages'], 5)
            if platform == 'telegram':
                self.assertEqual(observed['max_pages'], 1)
            if platform == 'youtube':
                self.assertEqual(observed['details'], 2)
            return report, posts

    async def test_bounded_native_rss_and_mailru_do_not_require_streaming_leases(self):
        for platform in ('rss', 'mailru'):
            with self.subTest(platform=platform):
                await self.execute_fixture(platform)

    async def test_bounded_native_youtube_and_telegram_keep_head_posts(self):
        for platform in ('youtube', 'telegram'):
            with self.subTest(platform=platform):
                await self.execute_fixture(platform)

    async def test_native_parser_exception_is_not_mislabeled_as_deadline(self):
        report, posts = await self.execute_fixture('rss', native_error=TypeError('private detail'))
        row = report['sources'][0]
        self.assertEqual(row['attempts'][-1]['tool'], 'source_execution')
        self.assertEqual(row['diagnostics'][-1]['exception_type'], 'TypeError')
        self.assertEqual(row['diagnostics'][-1]['phase'], 'extraction')
        result = coverage([{'id': 'fixture', 'platform': 'rss'}], report, posts)
        self.assertEqual(result['platforms']['rss']['failed_checks'], 1)
        self.assertNotIn('private detail', json.dumps(result))

    async def test_optional_metadata_exception_cannot_discard_verified_posts(self):
        report, posts = await self.execute_fixture('telegram', metrics_error=TypeError('private detail'))
        attempts = report['sources'][0]['attempts']
        self.assertEqual(attempts[-1]['status'], 'runtime_type_error')
        result = coverage([{'id': 'fixture', 'platform': 'telegram'}], report, posts)
        stats = result['platforms']['telegram']
        self.assertEqual(stats['completed_checks'], 1)
        self.assertEqual(stats['extracted_records'], 1)
        self.assertTrue(stats['error_diagnostics'])
        self.assertNotIn('private detail', json.dumps(result))


class OptionalMetricsTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_optional_metrics_preserve_verified_input(self):
        from post_metrics import bounded_refresh
        items = [{'canonical_key': 'telegram:fixture/1', 'text': 'Publication'}]
        async def slow(*args, **kwargs):
            await asyncio.sleep(10)
        with patch('asyncio.to_thread', slow):
            result, metadata = await bounded_refresh({'platform': 'telegram'}, items, {}, seconds=.01)
        self.assertEqual(result, items)
        self.assertEqual(metadata[0]['status'], 'deferred_budget')
