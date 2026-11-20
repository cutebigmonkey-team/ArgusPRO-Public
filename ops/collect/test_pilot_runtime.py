import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from pilot_runtime import LANES, assignment, advance_checkpoint, coverage, execute


def source(sid='rss-001', platform='rss', **extra):
    return dict(id=sid, platform=platform, url='https://example.org/feed', **extra)


class PilotTests(unittest.TestCase):
    def test_all_enabled_sources_have_exactly_one_lane_and_shard(self):
        sources = [source(f'{platform}-{i}', platform) for platform in set().union(*LANES.values()) for i in range(11)]
        sources.append(source('disabled', enabled=False))
        assigned = [s['id'] for lane in LANES for shard in range(4) for s in assignment(sources, lane, shard, 4)]
        self.assertEqual(len(assigned), len(set(assigned)))
        self.assertEqual(set(assigned), {s['id'] for s in sources if s.get('enabled', True)})

    def test_invalid_or_duplicate_sources_fail_closed(self):
        for rows in ([source(platform='unknown')], [source(), source()]):
            with self.assertRaises(ValueError):
                assignment(rows, 'fast', 0, 1)

    def test_rotation_preserves_membership_and_changes_budget_admission_order(self):
        rows = [source(f'rss-{i}') for i in range(4)]
        first = assignment(rows, 'fast', 0, 1, turn=0)
        second = assignment(rows, 'fast', 0, 1, turn=1)
        self.assertEqual({s['id'] for s in first}, {s['id'] for s in second})
        self.assertEqual(second[0], first[1])

    def test_unknown_status_never_exposes_arbitrary_collector_messages(self):
        result = coverage([source()], {'sources': [{'source_id': 'rss-001', 'status': 'sensitive raw message'}]}, [])
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertEqual(result['platforms']['rss']['statuses'], {'other_collector_error': 1})

    def test_extended_cannot_take_fast_sources(self):
        self.assertEqual(assignment([source()], 'extended', 0, 1), [])
        with self.assertRaises(ValueError):
            assignment([], 'invalid', 0, 1)

    def test_deferred_and_missing_sources_are_not_successful_checks(self):
        rows = [source('aaa'), source('bbb')]
        report = {'sources': [{'source_id': 'aaa', 'status': 'deferred_budget'}]}
        result = coverage(rows, report, [])
        stats = result['platforms']['rss']
        self.assertEqual(stats['assigned'], 2)
        self.assertEqual(stats['started'], 0)
        self.assertEqual(stats['statuses'], {'deferred_budget': 1, 'unreported': 1})
        self.assertEqual(result['unreported'], 1)

    def test_failed_collector_accounts_for_every_source(self):
        result = coverage([source()], {}, [], failed=True)
        self.assertEqual(result['platforms']['rss']['statuses'], {'collector_failed': 1})

    def test_archive_and_frontend_counts_are_unknown(self):
        result = coverage([source()], {}, [])
        self.assertEqual(result['platforms']['rss']['archive_writes'], 'NOT_MEASURABLE')
        self.assertFalse(result['checkpoint_advanced'])

    def test_freshness_requires_original_aware_publication_timestamp(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        posts = [{'platform': 'rss', 'published_at': stamp} for stamp in (None, '2026-11-17T11:59:00Z', '2026-11-17T11:59:00', '2020-01-01T00:00:00Z')]
        posts.append({'platform': 'rss', 'published_at': '2026-11-17T11:59:00Z', 'published_at_is_estimated': True})
        stats = coverage([source()], {}, posts, now=now)['platforms']['rss']
        self.assertEqual(stats['extracted_records'], 5)
        self.assertEqual(stats['dated_records'], 2)
        self.assertEqual(stats['publication_age_le_5m'], 1)

    def test_platform_health_preserves_reader_attempts_without_raw_messages(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 17, 12, tzinfo=timezone.utc)
        row = {'source_id': 'rss-001', 'status': 'partial_results', 'posts': 1,
               'started_at': '2026-11-17T11:59:00Z', 'latest_published_at': '2026-11-17T11:58:00Z',
               'attempts': [{'tool': 'rss_bridge', 'status': 'ok'},
                            {'tool': 'untrusted detail', 'status': 'untrusted detail'}]}
        stats = coverage([source()], {'sources': [row]}, [], now=now)['platforms']['rss']
        self.assertEqual(stats['started'], 1)
        self.assertEqual(stats['sources_with_posts'], 1)
        self.assertEqual(stats['sources_fresh_5m'], 1)
        self.assertEqual(stats['latest_source_age_s_median'], 120)
        self.assertEqual(stats['reader_attempts'], {'rss_bridge': {'ok': 1}, 'other_reader': {'other_collector_error': 1}})
        self.assertNotIn('untrusted detail', json.dumps(stats))

    def test_rotation_eventually_admits_every_source_with_single_source_budget(self):
        rows = [source(f'rss-{i}') for i in range(9)]
        admitted = {assignment(rows, 'fast', 0, 1, turn=i)[0]['id'] for i in range(len(rows))}
        self.assertEqual(admitted, {s['id'] for s in rows})

    def test_untrusted_receipts_never_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cursors.json'
            path.write_text('{"cursor":"preserved"}')
            original = path.read_bytes()
            for receipt in (None, {'archive_verified': True}, {'status': 200}):
                with self.assertRaisesRegex(RuntimeError, 'authenticated_archive_readback'):
                    advance_checkpoint(path, receipt)
                self.assertEqual(path.read_bytes(), original)

    def test_failure_exit_codes_and_cursor_proposals_remain_uncommitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / 'sources.json'
            sources.write_text(json.dumps({'sources': [source()]}))
            cursor = root / 'cursor.json'
            cursor.write_text('unchanged')
            def runner(command, **kwargs):
                self.assertIn('--incremental', command)
                self.assertIn('--adaptive', command)
                self.assertNotIn('--cursors', command)
                output = Path(command[command.index('--output') + 1])
                (output / 'report.json').write_text(json.dumps({'sources': [{'source_id': 'rss-001', 'status': 'deferred_budget'}], 'state_updates': {'rss-001': {'head_id': 'new'}}}))
                return SimpleNamespace(returncode=7)
            args = SimpleNamespace(sources=str(sources), lane='fast', shard_index=0, shard_count=1, output=str(root / 'out'))
            self.assertEqual(execute(args, runner), 7)
            self.assertEqual(cursor.read_text(), 'unchanged')
            self.assertFalse(json.loads((root / 'out/coverage.json').read_text())['checkpoint_advanced'])

    def test_successful_extraction_still_cannot_commit_unacknowledged_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sources.json').write_text(json.dumps({'sources': [source()]}))
            cursor = root / 'cursor.json'
            cursor.write_text('original checkpoint')
            def runner(command, **kwargs):
                output = Path(command[command.index('--output') + 1])
                (output / 'report.json').write_text(json.dumps({'sources': [{'source_id': 'rss-001', 'status': 'ok', 'started_at': '2026-11-17T12:00:00Z'}], 'state_updates': {'rss-001': {'head_id': 'new'}}}))
                (output / 'posts.jsonl').write_text(json.dumps({'platform': 'rss', 'published_at': None}) + '\n')
                return SimpleNamespace(returncode=0)
            args = SimpleNamespace(sources=str(root / 'sources.json'), lane='fast', shard_index=0, shard_count=1, output=str(root / 'out'))
            self.assertEqual(execute(args, runner), 0)
            result = json.loads((root / 'out/coverage.json').read_text())
            self.assertEqual(result['platforms']['rss']['extracted_records'], 1)
            self.assertFalse(result['checkpoint_advanced'])
            self.assertEqual(cursor.read_text(), 'original checkpoint')

    def test_native_shard_coordinates_preserve_per_shard_upstream_quotas(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = source()
            shard = int(hashlib.sha256(row['id'].encode()).hexdigest(), 16) % 4
            (root / 'sources.json').write_text(json.dumps({'sources': [row]}))
            def runner(command, **kwargs):
                self.assertEqual(command[command.index('--shard-index') + 1], str(shard))
                self.assertEqual(command[command.index('--shard-count') + 1], '4')
                assigned = json.loads(Path(command[command.index('--sources') + 1]).read_text())['sources']
                self.assertEqual(assigned, [row])
                output = Path(command[command.index('--output') + 1])
                (output / 'report.json').write_text(json.dumps({'sources': [{'source_id': row['id'], 'status': 'deferred_rate_limit'}]}))
                (output / 'posts.jsonl').write_text('')
                return SimpleNamespace(returncode=0)
            args = SimpleNamespace(sources=str(root / 'sources.json'), lane='fast', shard_index=shard, shard_count=4, output=str(root / 'out'))
            self.assertEqual(execute(args, runner), 0)
            stats = json.loads((root / 'out/coverage.json').read_text())['platforms']['rss']
            self.assertEqual(stats['statuses'], {'deferred_rate_limit': 1})

    def test_successful_process_requires_valid_report_and_post_artifacts(self):
        for malformed in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'sources.json').write_text(json.dumps({'sources': [source()]}))
                def runner(command, **kwargs):
                    output = Path(command[command.index('--output') + 1])
                    if malformed:
                        (output / 'report.json').write_text('invalid json')
                        (output / 'posts.jsonl').write_text('')
                    return SimpleNamespace(returncode=0)
                args = SimpleNamespace(sources=str(root / 'sources.json'), lane='fast', shard_index=0, shard_count=1, output=str(root / 'out'))
                self.assertEqual(execute(args, runner), 1)
                self.assertFalse(json.loads((root / 'out/coverage.json').read_text())['checkpoint_advanced'])

    def test_timeout_is_failure_with_explicit_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sources.json').write_text(json.dumps({'sources': [source()]}))
            def timeout(*args, **kwargs):
                raise subprocess.TimeoutExpired('collector', 240)
            args = SimpleNamespace(sources=str(root / 'sources.json'), lane='fast', shard_index=0, shard_count=1, output=str(root / 'out'))
            self.assertEqual(execute(args, timeout), 124)
            self.assertEqual(json.loads((root / 'out/coverage.json').read_text())['unreported'], 1)

    def test_empty_lane_is_valid_and_stale_results_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sources.json').write_text(json.dumps({'sources': []}))
            args = SimpleNamespace(sources=str(root / 'sources.json'), lane='fast', shard_index=0, shard_count=1, output=str(root / 'out'))
            self.assertEqual(execute(args, lambda *a, **kw: self.fail('empty lane ran collector')), 0)
            with self.assertRaisesRegex(ValueError, 'output_must_be_empty'):
                execute(args)

    def test_unexpected_or_duplicate_report_sources_rejected(self):
        for rows in ([{'source_id': 'other'}], [{'source_id': 'rss-001'}] * 2):
            with self.assertRaises(ValueError):
                coverage([source()], {'sources': rows}, [])

    def test_workflows_keep_pr_jobs_unprivileged_and_lanes_independent(self):
        root = Path(__file__).resolve().parents[2]
        workflow = (root / '.github/workflows/argus-public-collection.yml').read_text()
        lane = (root / '.github/workflows/argus-collection-lane.yml').read_text()
        self.assertNotIn('pull_request_target', workflow + lane)
        self.assertNotIn('concurrency:', workflow)
        self.assertNotIn('needs:', workflow)
        self.assertIn('argus-pilot-${{ inputs.lane }}-${{ matrix.shard }}', lane)
        self.assertEqual(workflow.count("github.event_name != 'pull_request'"), 2)
        tests = workflow.split('  tests:', 1)[1].split('  fast:', 1)[0]
        self.assertNotIn('secrets.', tests)
        self.assertIn('contents: read', workflow)
        self.assertEqual(workflow.count('validation_only: true'), 2)
        self.assertIn("!inputs.validation_only && github.event_name != 'pull_request'", lane)
        fixture_job = lane.split('  validate:', 1)[1].split('  collect:', 1)[0]
        self.assertNotIn('secrets.', fixture_job)
        self.assertNotIn('pip install', fixture_job)
        self.assertNotIn('secrets: inherit', workflow + lane)
