"""Isolated, dependency-injected ARGUS HTTP ingress; NOT a deployed service.

The gateway verifies GitHub OIDC before accepting a *canonical* collector
candidate; server-side signing is deliberately not delegated to GitHub Actions.
It does not import anything into the archive and has no credentials for it.
"""
import hashlib
import hmac
import json
import re

from envelope import MAX_BYTES, _reject_duplicates, canonical, validate
from oidc_verifier import authorize_envelope


class IngestionGateway:
    def __init__(self, queue, *, server_key, authorize=authorize_envelope):
        if not isinstance(server_key, bytes) or len(server_key) < 32:
            raise ValueError('invalid_server_signing_key')
        self.queue = queue
        self._server_key = server_key
        self._authorize = authorize

    def accept(self, token, body):
        """Return queue receipt, never an archive acknowledgement."""
        if not isinstance(body, bytes) or not body or len(body) > MAX_BYTES:
            raise ValueError('invalid_candidate_size')
        try:
            payload = json.loads(body.decode('utf-8'), object_pairs_hook=_reject_duplicates,
                                 parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_number')))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('invalid_candidate_json') from exc
        if canonical(payload) != body:
            raise ValueError('noncanonical_candidate')
        validate(payload)
        identity = self._authorize(token, payload)
        if not isinstance(identity, dict) or identity.get('run_id') != payload['run_id'].split(':')[0] or identity.get('run_attempt') != payload['run_id'].split(':')[1]:
            raise ValueError('untrusted_workflow_identity')
        digest = hashlib.sha256(body).hexdigest()
        signature = hmac.new(self._server_key, body, hashlib.sha256).hexdigest()
        result = self.queue.submit(body, sha256=digest, signature=signature, key=self._server_key)
        return {'status': result, 'run_id': payload['run_id'], 'sha256': digest,
                'archive_verified': False}


def wsgi_application(gateway):
    """WSGI app definition only. Deployment TLS termination is external.

    The TLS requirement uses the authenticated server-side WSGI scheme;
    never trust X-Forwarded-Proto from the HTTP client directly.
    """
    def application(environ, start_response):
        def reply(code, data):
            content = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
            start_response(code, [('Content-Type', 'application/json'),
                                  ('Content-Length', str(len(content))),
                                  ('Cache-Control', 'no-store')])
            return [content]

        if environ.get('PATH_INFO') != '/v1/ingestion/batches':
            return reply('404 Not Found', {'error': 'not_found'})
        if environ.get('REQUEST_METHOD') != 'POST':
            return reply('405 Method Not Allowed', {'error': 'method_not_allowed'})
        if environ.get('wsgi.url_scheme') != 'https':
            return reply('403 Forbidden', {'error': 'https_required'})
        if environ.get('CONTENT_TYPE', '').split(';')[0].strip().lower() != 'application/json':
            return reply('415 Unsupported Media Type', {'error': 'json_required'})
        length = environ.get('CONTENT_LENGTH', '')
        if not isinstance(length, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', length) or int(length) > MAX_BYTES:
            return reply('413 Content Too Large', {'error': 'invalid_content_length'})
        auth = environ.get('HTTP_AUTHORIZATION', '')
        if not isinstance(auth, str) or not auth.startswith('Bearer ') or not auth[7:] or len(auth) > 17000:
            return reply('401 Unauthorized', {'error': 'unauthorized'})
        stream = environ.get('wsgi.input')
        if stream is None:
            return reply('400 Bad Request', {'error': 'invalid_body'})
        body = stream.read(int(length))
        if len(body) != int(length):
            return reply('400 Bad Request', {'error': 'incomplete_body'})
        try:
            receipt = gateway.accept(auth[7:], body)
        except Exception:
            # Never leak JWT claims, cryptographic details, catalog or post content.
            return reply('401 Unauthorized', {'error': 'candidate_rejected'})
        return reply('202 Accepted', receipt)
    return application
