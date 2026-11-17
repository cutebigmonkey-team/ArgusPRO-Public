"""ARGUS unprivileged ingestion envelope: local contract, no network or archive writes.

Design: authenticate a bounded JSON document over its exact canonical bytes,
validate the payload, and hand it to the *existing* fenced writer only after a
separate authenticated endpoint has verified transport identity and replay state.
"""
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone, timedelta

MAX_BYTES = 2_000_000
MAX_POSTS = 250
MAX_SOURCES = 250
SCHEMA = 'argus-ingest-envelope-v1'
KEY_PATTERN = re.compile(r'^[a-z0-9][a-z0-9:_./-]{2,200}$', re.I)
PLATFORMS = {'telegram','youtube','x','facebook','instagram','rss','tiktok','threads','vk','ok','reddit','rutube','mailru','gdelt'}


def _reject_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate_json_key')
        out[key] = value
    return out


def _iso(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('invalid_timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as err:
        raise ValueError('invalid_timestamp') from err
    if parsed.tzinfo is None:
        raise ValueError('missing_timezone')
    return parsed.astimezone(timezone.utc)


def canonical(payload):
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def validate(payload, *, now=None):
    """Validate *untrusted* document before it enters any queue or journal."""
    if not isinstance(payload, dict) or set(payload) != {'schema','run_id','shard','lane','created_at','sources','posts'}:
        raise ValueError('invalid_envelope_shape')
    if payload['schema'] != SCHEMA:
        raise ValueError('invalid_schema')
    run_id = payload['run_id']
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9]{1,20}:[0-9]{1,2}:(fast|extended):[0-3]', run_id):
        raise ValueError('invalid_run_id')
    parts = run_id.split(':')
    if type(payload['shard']) is not int or payload['shard'] not in range(4) or payload['shard'] != int(parts[-1]):
        raise ValueError('invalid_shard')
    if payload['lane'] not in ('fast','extended') or payload['lane'] != parts[-2]:
        raise ValueError('invalid_lane')
    when = _iso(payload['created_at'])
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    if not now - timedelta(hours=24) <= when <= now + timedelta(minutes=5):
        raise ValueError('stale_or_future_envelope')
    sources = payload['sources'];posts = payload['posts']
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES or not isinstance(posts, list) or len(posts) > MAX_POSTS:
        raise ValueError('invalid_batch_size')
    ids = set()
    for item in sources:
        if not isinstance(item, dict) or set(item) != {'source_id','platform','status','posts'}:
            raise ValueError('invalid_source_record')
        sid = item['source_id']
        if not isinstance(sid, str) or not KEY_PATTERN.fullmatch(sid) or sid in ids:
            raise ValueError('invalid_source_id')
        ids.add(sid)
        if item['platform'] not in PLATFORMS or not isinstance(item['status'], str) or len(item['status']) > 64 or type(item['posts']) is not int or not 0 <= item['posts'] <= MAX_POSTS:
            raise ValueError('invalid_source_metadata')
    keys = set()
    for post in posts:
        if not isinstance(post, dict) or 'canonical_key' not in post or 'source_id' not in post or 'platform' not in post:
            raise ValueError('invalid_post')
        key = post['canonical_key']
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or key in keys:
            raise ValueError('invalid_canonical_key')
        keys.add(key)
        if post['source_id'] not in ids or post['platform'] not in PLATFORMS:
            raise ValueError('unknown_source_or_platform')
        if 'published_at' in post and post['published_at'] is not None:
            _iso(post['published_at'])
    data = canonical(payload)
    if len(data) > MAX_BYTES:
        raise ValueError('payload_too_large')
    return data


def sign(payload, key):
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('weak_signing_key')
    data = validate(payload)
    return hashlib.sha256(data).hexdigest(), hmac.new(key, data, hashlib.sha256).hexdigest()


def verify(body, *, sha256, signature, key, now=None):
    if not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError('oversized_body')
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('weak_signing_key')
    if not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise ValueError('invalid_digest')
    if not isinstance(signature, str) or not re.fullmatch(r'[0-9a-f]{64}', signature):
        raise ValueError('invalid_signature')
    actual = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(actual, sha256):
        raise ValueError('digest_mismatch')
    expected = hmac.new(key, body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ValueError('authentication_failed')
    try:
        payload = json.loads(body.decode('utf-8'), object_pairs_hook=_reject_duplicates,
                             parse_constant=lambda _x: (_ for _ in ()).throw(ValueError('invalid_number')))
    except (UnicodeError, json.JSONDecodeError) as e:
        raise ValueError('invalid_json') from e
    if canonical(payload) != body:
        raise ValueError('noncanonical_payload')
    validate(payload, now=now)
    return payload
