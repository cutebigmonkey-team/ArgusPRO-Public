"""Production-disabled private admission boundary; no listener or production DB setup."""
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit

from envelope import MAX_BYTES
from oidc_policy import REPO, REF
from oidc_verifier import verify_token
from partition import decode

REUSABLE = f'{REPO}/.github/workflows/argus-collection-lane.yml@{REF}'
FORBIDDEN = frozenset(('access_token','refresh_token','authorization','cookie','cookies',
    'password','client_secret','private_key','oauth_token','sessionid','lease_token','_lease_token'))


def safe_input(value, depth=0):
    if depth>32:raise ValueError('input_too_deep')
    if isinstance(value,dict):
        if any(k.lower() in FORBIDDEN for k in value):raise ValueError('credential_fields_forbidden')
        for item in value.values():safe_input(item,depth+1)
    elif isinstance(value,list):
        for item in value:safe_input(item,depth+1)
    elif isinstance(value,str) and value.startswith(('https://','http://')):
        parsed=urlsplit(value)
        if parsed.username or parsed.password:raise ValueError('credential_url_forbidden')


class PrivateGateway:
    """A trusted private HTTP adapter supplies TLS, rate limiting and connection pool.

    source_policy must validate against a private registry, including cursor shape
    and source URL ownership. No catalog or policy is supplied by public requests.
    The supplied store exposes ingress methods only; never expose a writer role.
    """
    def __init__(self, store, source_policy, *, repository_id, owner_id, enabled=False, key_client=None):
        self.store=store;self.source_policy=source_policy;self.enabled=enabled
        self.repository_id=repository_id;self.owner_id=owner_id;self.key_client=key_client
        if not callable(source_policy) or not repository_id or not owner_id:raise ValueError('private_policy_required')

    def _identity(self, token, now):
        identity=verify_token(token,key_client=self.key_client,now=now)
        if (identity['repository_id']!=self.repository_id or identity['repository_owner_id']!=self.owner_id
            or identity['job_workflow_ref']!=REUSABLE or identity['job_workflow_sha']!=identity['sha']
            or identity['workflow_sha']!=identity['sha']):
            raise ValueError('untrusted_workflow_identity')
        return identity

    def admit(self, body, *, authorization, content_type='application/json', now=None):
        if not self.enabled:raise ValueError('production_ingestion_disabled')
        if content_type!='application/json' or not isinstance(body,bytes) or len(body)>MAX_BYTES:
            raise ValueError('invalid_http_body')
        if not isinstance(authorization,str) or not authorization.startswith('Bearer ') or len(authorization)>16400:
            raise ValueError('invalid_authorization')
        identity=self._identity(authorization[7:],now)
        part=decode(body,now=now);producer=part['producer']
        if (str(producer['run_id'])!=identity['run_id'] or str(producer['attempt'])!=identity['run_attempt']
            or producer['sha']!=identity['sha']):raise ValueError('producer_identity_mismatch')
        # Match the native collector assignment, not a caller-selected lane/shard.
        from_source=part['source'];sid=from_source['source_id']
        from_lanes={'fast':{'telegram','rss','mailru','youtube','threads','tiktok','rutube','reddit','gdelt'},
                    'extended':{'facebook','instagram','vk','ok','x'}}
        if from_source['platform'] not in from_lanes[producer['lane']] or int(hashlib.sha256(sid.encode()).hexdigest(),16)%4!=producer['shard']:
            raise ValueError('source_assignment_mismatch')
        safe_input(part)
        if self.source_policy(part,identity) is not True:raise ValueError('unapproved_source')
        result=self.store.submit_part(body,jti_hash=hashlib.sha256(identity['jti'].encode()).hexdigest(),
            expires_at=datetime.fromtimestamp(identity['expires_at'],timezone.utc),now=now)
        if result not in ('queued','already_queued','already_acknowledged'):raise ValueError('durable_queue_receipt_required')
        return result

    def recover(self, source_id, *, token, now=None):
        if not self.enabled:raise ValueError('production_ingestion_disabled')
        identity=self._identity(token,now)
        if self.source_policy({'recover_source_id':source_id},identity) is not True:
            raise ValueError('unapproved_source')
        return {'source_id':source_id,**self.store.recover_cursor(source_id)}

    def http(self, environ, start_response):
        """Optional WSGI boundary, never bound to a listener by this package."""
        if not self.enabled:
            start_response('503 Service Unavailable',[('Content-Type','application/json')]);return [b'{"error":"ingestion_disabled"}']
        try:
            length=environ.get('CONTENT_LENGTH','')
            if environ.get('PATH_INFO')!='/v2/ingest' or environ.get('REQUEST_METHOD')!='POST' or not length.isdigit() or not 0<int(length)<=MAX_BYTES:
                raise ValueError('invalid_request')
            body=environ['wsgi.input'].read(int(length))
            if len(body)!=int(length):raise ValueError('body_length_mismatch')
            result=self.admit(body,authorization=environ.get('HTTP_AUTHORIZATION'),content_type=environ.get('CONTENT_TYPE'),now=None)
            start_response('202 Accepted',[('Content-Type','application/json')])
            # Queue receipt explicitly does not attest archive persistence.
            return [b'{"queued":true,"archive_verified":false}']
        except ValueError as error:
            if str(error)=='ingestion_backpressure':
                start_response('503 Service Unavailable',[('Content-Type','application/json'),('Retry-After','30')]);return [b'{"error":"ingestion_backpressure"}']
            start_response('403 Forbidden',[('Content-Type','application/json')]);return [b'{"error":"ingestion_rejected"}']
        except Exception:
            start_response('503 Service Unavailable',[('Content-Type','application/json'),('Retry-After','30')]);return [b'{"error":"ingestion_unavailable"}']
