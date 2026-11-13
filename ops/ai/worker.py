"""Scheduled, single-writer async enrichment of the existing private Drive archive.

Every outbound attempt is durably reserved BEFORE sending. An interrupted or
ambiguous attempt requires explicit operator review, never automatic replay.
Run under the shared Hostless writer session lock or the legacy serialized workflow.
"""
import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'drive'))
from drive_store import from_environment, encoded
from publish import load_snapshot
from typesafe import ApiError, Client, MODEL, VERSION, request_body, validate
from reporting import build_reports, timestamp
from privacy import official_channels, verified_public, verified_public_archive_text, RESTRICTED

LEDGER = 'typesafe-ledger-v1.json'
POINTER = 'typesafe-catalog.json'


def sha(value): return hashlib.sha256(encoded(value)).hexdigest()


def content_hash(item):
    # An edit to any publication field invalidates its egress approval.
    return sha(item)


def approved_text(item, policy, now, public_dsns_consent=False, channels=frozenset(), public_archive_consent=False):
    if RESTRICTED.search(str(item.get('text', ''))) or item.get('confidential') or item.get('internal'):
        return None
    if public_dsns_consent is True and verified_public(item, channels):
        return item['text']
    if public_archive_consent is True and verified_public_archive_text(item):
        return item['text']
    approval = policy.get('publications', {}).get(item['id'], {})
    if item.get('sample') or item.get('capture_mode') == 'search_preview' or item.get('text_may_be_truncated'):
        return None
    if approval.get('content_sha256') != content_hash(item): return None
    if approval.get('classification') != 'public_nonpersonal' or approval.get('approved') is not True:
        return None
    if not approval.get('reviewer') or not approval.get('reviewed_at'): return None
    try:
        expires = dt.datetime.fromisoformat(approval['expires_at'].replace('Z', '+00:00'))
        reviewed = dt.datetime.fromisoformat(approval['reviewed_at'].replace('Z', '+00:00'))
        if not expires.tzinfo or not reviewed.tzinfo or expires.timestamp() <= now or reviewed.timestamp() > now: return None
    except (KeyError, ValueError, TypeError): return None
    text = approval.get('text')
    if not isinstance(text, str) or not text.strip(): return None
    # Secondary guard only; human review of names/locations/sensitive details
    # is still required. Never pretend regex is a complete PII detector.
    if re.search(r'https?://|[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}|(?:\+?\d[\s().-]*){9,}|(?:api[_ -]?key|bearer|password)\s*[:=]', text, re.I):
        return None
    return text


class Worker:
    def __init__(self, store, client, config=None, clock=time.time, guard=lambda: None, pointer=POINTER):
        self.store, self.client, self.clock = store, client, clock
        self.guard = guard
        if pointer not in (POINTER,'comments-typesafe-catalog.json'):raise ValueError('invalid_analysis_pointer')
        self.pointer=pointer
        self.config = config or {}
        self.model = self.config.get('model', MODEL)
        self.public_dsns_consent = self.config.get('public_dsns_consent') is True
        self.public_archive_consent = self.config.get('public_archive_consent') is True
        self.channels = official_channels() if self.public_dsns_consent else frozenset()
        if self.model not in ('jev-1.14.0', 'jev-1.13.0', 'jev-latest', 'jev-preview'): raise ValueError('Select a documented TypeSafe model')
        self.batch_size = min(4, max(1, int(self.config.get('concurrency', 2))))
        self.max_posts = min(500, max(1, int(self.config.get('max_posts', 50))))
        self.max_bytes = int(self.config.get('max_request_bytes', 24000))
        if not 1 <= self.max_bytes <= 65536: raise ValueError('Invalid request byte limit')
        self.rps = float(self.config.get('requests_per_second', 1))
        self.tps = float(self.config.get('tokens_per_second', 1000))
        self.daily_tokens = int(self.config.get('daily_input_tokens', 100000))
        self.daily_requests = int(self.config.get('daily_requests', 100))
        if not 0 < self.rps <= 100 or not 0 < self.tps <= 1000000 or not 0 < self.daily_tokens <= 100000000 or not 0 < self.daily_requests <= 100000:
            raise ValueError('Invalid budget or rate limit')
        self.gate = asyncio.Lock()
        self.next_start = 0

    def save(self):
        # The serialized writer can safely repeat this identical checkpoint,
        # including when Drive committed it but the connection lost the reply.
        # Retry storage only: never replay a TypeSafe request here.
        started = time.monotonic()
        try:
            for attempt in range(4):
                try:
                    self.guard()
                    self.store.json(LEDGER, self.ledger, mutable=True)
                    return
                except (ConnectionError, TimeoutError):
                    if attempt == 3: raise
                    time.sleep(2 ** attempt)
        finally:
            self.timings['checkpoint_seconds'] += time.monotonic() - started

    async def request(self, key, body):
        async with self.gate:
            await asyncio.sleep(max(0, self.next_start - time.monotonic()))
            self.next_start = time.monotonic() + max(1 / self.rps, len(body) / self.tps)
        try:
            self.guard()
            started = time.monotonic()
            raw = await asyncio.to_thread(self.client.call, body)
            result = validate(raw)
            return key, {'status': 'completed', 'result': result}
        except ApiError as exc:
            if exc.code in (429, 529):
                n = self.ledger['jobs'][key]['attempts']
                delay = max(exc.retry_after, min(3600, 30 * 2 ** n) + random.uniform(0, 10))
                return key, {'status': 'retry' if n < 4 else 'failed', 'error': str(exc.code), 'retry_at': self.clock() + delay}
            return key, {'status': 'failed' if exc.code in (400, 401, 403, 413, 415, 422) else 'uncertain', 'error': str(exc.code)}
        except (ValueError, TypeError, KeyError):
            return key, {'status': 'uncertain', 'error': 'invalid_response'}
        finally:
            if 'started' in locals(): self.timings['api_seconds_sum'] += time.monotonic() - started

    async def run(self, snapshot, policy, target_ids=None, max_seconds=None, request_seconds=None):
        started = time.monotonic()
        deadline = started + max_seconds if max_seconds is not None else float('inf')
        self.timings = {'checkpoint_seconds': 0.0, 'api_seconds_sum': 0.0}
        self.guard()
        self.store.assert_private()
        self.ledger = self.store.read_json(LEDGER) or {'format': 'argus-typesafe-ledger-v1', 'jobs': {}, 'days': {}}
        if self.ledger.get('format') != 'argus-typesafe-ledger-v1': raise ValueError('Invalid ledger')
        now = self.clock()
        self.next_start = time.monotonic() + max(0, self.ledger.get('next_request_at', 0) - now)
        day = dt.datetime.fromtimestamp(now, dt.timezone.utc).date().isoformat()
        budget = self.ledger['days'].setdefault(day, {'reserved_tokens': 0, 'requests': 0, 'input_tokens': 0, 'output_tokens': 0})
        requests_before = budget['requests']
        new_completed = 0
        bindings, statuses, pending = {}, {}, {}
        recent = {}
        for item in snapshot['items']:
            text = approved_text(item, policy, now, self.public_dsns_consent, self.channels, self.public_archive_consent)
            if text is None:
                statuses[item['id']] = 'blocked_privacy'
                continue
            try: body = request_body(text, self.model, self.max_bytes)
            except ValueError:
                statuses[item['id']] = 'payload_limit'
                continue
            key = hashlib.sha256(body + VERSION.encode()).hexdigest()
            bindings[item['id']] = key
            if target_ids is not None and item['id'] not in target_ids: continue
            job = self.ledger['jobs'].get(key, {})
            if not job or (job.get('status') == 'retry' and job.get('retry_at', 0) <= now):
                # Retain a reference to the existing text, not a second encoded
                # request for every archived post on the small cloud worker.
                pending[key] = (text, len(body))
                published = timestamp(item.get('published_at'))
                if published is not None and now - 7 * 86400 <= published <= now:
                    recent[key] = max(recent.get(key, published), published)
        # Prioritize this week's report. Keep archive order for the backlog.
        # A long request that cannot fit today must not block a smaller one.
        remaining = self.daily_tokens - budget['reserved_tokens']
        slots = min(self.max_posts if target_ids is None else 500, self.daily_requests - budget['requests'])
        keys = []
        if now >= self.ledger.get('pause_until', 0):
            for key in sorted(pending, key=lambda k: recent.get(k, float('-inf')), reverse=True):
                if len(keys) >= slots: break
                size = pending[key][1]
                if size <= remaining:
                    keys.append(key)
                    remaining -= size
        stop = False
        request_started = time.monotonic()
        self.timings['prepare_seconds'] = request_started - started
        if request_seconds is not None: deadline = min(deadline, request_started + request_seconds)
        stop_reason = 'request_cap' if keys else ('provider_backoff' if now < self.ledger.get('pause_until', 0)
            else 'daily_budget' if pending else 'no_eligible')
        errors = {}
        # Yield the shared archive writer with time to publish the full archive
        # product. A later dispatch resumes from the durable request ledger.
        if target_ids is not None: deadline = min(deadline, time.monotonic() + 8*60)
        for offset in range(0, len(keys), self.batch_size):
            # Do not reserve a wave that would wait beyond this cloud slot.
            if max(time.monotonic(), self.next_start) >= deadline:
                stop_reason = 'slot_deadline'
                break
            self.guard()
            wave = []
            for key in keys[offset:offset + self.batch_size]:
                body = request_body(pending[key][0], self.model, self.max_bytes)
                if budget['requests'] >= self.daily_requests or budget['reserved_tokens'] + len(body) > self.daily_tokens:
                    stop = True
                    stop_reason = 'daily_budget'
                    break
                old = self.ledger['jobs'].get(key, {})
                budget['requests'] += 1
                budget['reserved_tokens'] += len(body)
                self.ledger['next_request_at'] = max(self.clock(), self.ledger.get('next_request_at', 0)) + max(1/self.rps, len(body)/self.tps)
                self.ledger['jobs'][key] = {'status': 'inflight', 'attempts': old.get('attempts', 0) + 1,
                    'reserved_tokens': len(body), 'updated_at': now}
                wave.append((key, body))
            # Checkpoint failure aborts before ANY request in this wave.
            self.save()
            results = await asyncio.gather(*(self.request(k, b) for k, b in wave))
            for key, result in results:
                self.ledger['jobs'][key].update(result, updated_at=self.clock())
                if result['status'] == 'completed':
                    new_completed += 1
                    usage = result['result']['usage']
                    budget['input_tokens'] += usage['input_tokens']
                    budget['output_tokens'] += usage['output_tokens']
                    # Keep conservative reservations, including failed calls.
                    if usage['input_tokens'] > self.ledger['jobs'][key]['reserved_tokens']:
                        budget['reserved_tokens'] += usage['input_tokens'] - self.ledger['jobs'][key]['reserved_tokens']
                        stop = True
                if result.get('error'):
                    code = result['error']
                    errors[code] = errors.get(code, 0) + 1
                if result.get('error') in ('401', '403', '429', '529'):
                    stop = True
                    stop_reason = 'provider_backoff' if result['error'] in ('429', '529') else 'provider_error'
                if result.get('error') in ('429', '529'):
                    self.ledger['pause_until'] = max(self.ledger.get('pause_until', 0), result['retry_at'])
            self.save()
            if stop: break
        self.timings['request_window_seconds'] = time.monotonic() - request_started
        product_started = time.monotonic()
        assessments = {}
        for item in snapshot['items']:
            key = bindings.get(item['id'])
            job = self.ledger['jobs'].get(key, {})
            status = statuses.get(item['id'], job.get('status', 'pending_budget'))
            record = {'content_sha256': content_hash(item), 'status': 'uncertain' if status == 'inflight' else status}
            record['official_source'] = verified_public(item, self.channels)
            if status == 'completed': record.update(job['result'])
            assessments[item['id']] = record
        updated = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc).isoformat()
        snapshot_digest = sha(snapshot)
        self.timings['binding_seconds'] = time.monotonic() - product_started
        reports_started = time.monotonic()
        reports = build_reports(snapshot, assessments, updated, policy.get('report_requests', []))
        self.timings['report_seconds'] = time.monotonic() - reports_started
        publish_started = time.monotonic()
        product = {'format': 'argus-typesafe-v1', 'snapshot_sha256': snapshot_digest, 'updated_at': updated,
            'public_dsns_consent': self.public_dsns_consent,
            'public_archive_consent': self.public_archive_consent,
            'assessments': assessments, 'budget': budget,
            'estimated_reserved_usd': round(budget['reserved_tokens'] * .042 / 1000000, 8),
            'reports': reports}
        # Separate immutable derived snapshot: never rewrite the collector index.
        digest = sha(product)
        name = 'typesafe-' + digest + '.json'
        self.guard()
        file_id = self.store.json(name, product)
        if self.store.read_json(name) != product: raise ValueError('typesafe_readback_failed')
        pointer = {'format': 'argus-typesafe-catalog-v1', 'id': file_id, 'sha256': digest,
            'snapshot_sha256': snapshot_digest, 'updated_at': updated}
        self.guard()
        self.store.json(self.pointer, pointer, mutable=True)
        if self.store.read_json(self.pointer) != pointer: raise ValueError('typesafe_pointer_readback_failed')
        self.timings['publish_seconds'] = time.monotonic() - publish_started
        self.timings['total_seconds'] = time.monotonic() - started
        return {'posts': len(assessments), 'completed': sum(r['status'] == 'completed' for r in assessments.values()),
                'new_completed': new_completed, 'requests_this_run': budget['requests'] - requests_before,
                'requests_today': budget['requests'], 'reserved_tokens': budget['reserved_tokens'],
                'pending_eligible': sum(r['status'] == 'pending_budget' for r in assessments.values()),
                'updated_at': updated, 'sha256': digest, 'stop_reason': stop_reason, 'request_errors': errors,
                'timings': {k: round(v, 3) for k, v in self.timings.items()}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-models', action='store_true')
    parser.add_argument('--smoke-test', action='store_true')
    args = parser.parse_args()
    client = Client()
    if args.check_models:
        data = client.call()
        print(json.dumps({'models': data.get('models', [])}, ensure_ascii=False))
        return
    if args.smoke_test:
        result = validate(client.call(request_body('Синтетичний тест інтеграції, не реальна подія. Навчальний матеріал про правила пожежної безпеки.')))
        print(json.dumps({'contract_valid': True, 'model': result['model'], 'usage': result['usage']}))
        return
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise ValueError('Use the serialized cloud workflow')
    store = from_environment()
    store.assert_private()
    policy = store.read_json('typesafe-policy.json') or {}
    config = json.loads(os.environ.get('ARGUS_TYPESAFE_CONFIG', '{}'))
    digest = os.environ.get('ARGUS_TYPESAFE_COHORT', '')
    target_ids = None
    if digest:
        if not re.fullmatch('[a-f0-9]{64}', digest): raise ValueError('Invalid cohort digest')
        manifest = store.read_json('typesafe-cohort-'+digest+'.json')
        if not isinstance(manifest,dict) or sha(manifest)!=digest or manifest.get('format')!='argus-typesafe-cohort-v1':
            raise ValueError('Invalid cohort manifest')
        ids=manifest.get('ids')
        if not isinstance(ids,list) or not ids or not all(isinstance(i,str) and i for i in ids):
            raise ValueError('Invalid cohort IDs')
        target_ids=set(ids)
    result = asyncio.run(Worker(store, client, config).run(load_snapshot(store), policy, target_ids))
    print(json.dumps(result))


if __name__ == '__main__': main()
