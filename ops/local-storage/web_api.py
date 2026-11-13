"""Local-only WSGI archive: cookie sessions, no tunnel or external API support."""
import argparse
from collections import deque
import hashlib
from http import HTTPStatus
from http.cookies import SimpleCookie
import json
import mimetypes
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from archive_api import Archive, PREFIX, SESSION_SECONDS, byte_range, media_type
from auth_store import AuthStore
from storage import read_json, safe_child, start_database

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

class App:
    def __init__(self, root, config, port=8765, archive=None):
        self.root = Path(root).resolve()
        self.archive = archive or Archive(self.root, config)
        self.auth = AuthStore(safe_child(self.root / 'private', config.get('auth_file', 'users.sqlite3')))
        self.local_origin = f'http://127.0.0.1:{port}'
        if config.get('public_origin') or config.get('frontend_origin'):
            raise ValueError('This build is local-only; external API origins are not supported')
        self.origins = {self.local_origin}
        self.hosts = {urlsplit(self.local_origin).netloc}
        self.cookie_name = 'argus_archive_session_' + str(port)
        self.sessions = {}
        self.attempts = deque()
        self.lock = threading.RLock()
        self.hash_slots = threading.BoundedSemaphore(2)

    def cleanup(self):
        now = time.time()
        for sid in [sid for sid, record in self.sessions.items() if record['expires'] <= now]:
            del self.sessions[sid]

    def session_id(self, env):
        if env.get('HTTP_HOST') == urlsplit(self.local_origin).netloc:
            cookie = SimpleCookie()
            try:
                cookie.load(env.get('HTTP_COOKIE', ''))
                if self.cookie_name in cookie:
                    return digest(cookie[self.cookie_name].value)
            except Exception:
                pass
        return ''

    def active(self, sid):
        with self.lock:
            self.cleanup()
            record = self.sessions.get(sid)
        return record if record and self.auth.active(record) else None

    def __call__(self, env, start_response):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                   'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'",
                   'Vary': 'Origin'}
        request_origin = env.get('HTTP_ORIGIN', '')
        method = env['REQUEST_METHOD']

        def reply(code, value, extra=None):
            body = json.dumps(value, ensure_ascii=False).encode()
            combined = {**headers, 'Content-Type': 'application/json; charset=utf-8',
                        'Content-Length': str(len(body)), **(extra or {})}
            start_response(f'{code} {HTTPStatus(code).phrase}', list(combined.items()))
            return [] if method == 'HEAD' else [body]

        if env.get('HTTP_HOST') not in self.hosts or (request_origin and request_origin not in self.origins):
            return reply(403, {'error': 'Untrusted host or origin'})
        path = env.get('PATH_INFO', '/')
        query = parse_qs(env.get('QUERY_STRING', ''))
        try:
            if method == 'OPTIONS':
                return reply(403, {'error': 'Cross-origin access is disabled'})
            if method == 'POST':
                if request_origin not in self.origins:
                    return reply(403, {'error': 'Origin required'})
                if path == PREFIX + '/login':
                    with self.lock:
                        now = time.monotonic()
                        while self.attempts and self.attempts[0] < now - 60:
                            self.attempts.popleft()
                        if len(self.attempts) >= 20:
                            return reply(429, {'error': 'Try again later'})
                        self.attempts.append(now)
                    length = int(env.get('CONTENT_LENGTH') or 0)
                    if not 0 < length <= 2048 or env.get('CONTENT_TYPE') != 'application/json':
                        return reply(400, {'error': 'Expected small JSON request'})
                    data = read_json(env['wsgi.input'].read(length))
                    if not self.hash_slots.acquire(blocking=False):
                        return reply(429, {'error': 'Try again later'})
                    try:
                        identity = self.auth.authenticate(data.get('username', ''), data.get('key', ''))
                    finally:
                        self.hash_slots.release()
                    if not identity:
                        return reply(401, {'error': 'Invalid login or key'})
                    token = secrets.token_urlsafe(32)
                    with self.lock:
                        self.cleanup()
                        if len(self.sessions) >= 256:
                            del self.sessions[next(iter(self.sessions))]
                        self.sessions[digest(token)] = {**identity, 'expires': time.time() + SESSION_SECONDS}
                    extra = {}
                    if env.get('HTTP_HOST') == urlsplit(self.local_origin).netloc:
                        extra['Set-Cookie'] = f'{self.cookie_name}={token}; HttpOnly; SameSite=Strict; Path={PREFIX}; Max-Age={SESSION_SECONDS}'
                    return reply(200, {'authenticated': True, 'username': identity['username'],
                                       'expires_in': SESSION_SECONDS}, extra)
                if path == PREFIX + '/logout':
                    with self.lock:
                        self.sessions.pop(self.session_id(env), None)
                        self.cleanup()
                    return reply(200, {'authenticated': False}, {'Set-Cookie': f'{self.cookie_name}=; HttpOnly; SameSite=Strict; Path={PREFIX}; Max-Age=0'})
                return reply(405, {'error': 'Read-only API'})
            if method not in ('GET', 'HEAD'):
                return reply(405, {'error': 'Method not allowed'})
            if path.startswith(PREFIX + '/'):
                sid = self.session_id(env)
                sha = path[len(PREFIX + '/media/'):] if path.startswith(PREFIX + '/media/') else None
                identity = self.active(sid)
                if not identity:
                    return reply(401, {'error': 'Authentication required'})
                if path == PREFIX + '/session':
                    return reply(200, {'authenticated': True, 'username': identity['username'],
                                       'archive_label': self.archive.config.get('label', 'Argus')})
                if path == PREFIX + '/materials':
                    data = self.archive.materials(query)
                    return reply(200, data)
                if sha:
                    file = self.archive.media(sha)
                    if not file:
                        return reply(404, {'error': 'Attachment not found'})
                    name = query.get('name', [file.name])[0].replace('\\', '/').split('/')[-1]
                    name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:160] or file.name
                    mime = media_type(file)
                    disposition = 'attachment' if mime == 'application/octet-stream' or 'download' in query else 'inline'
                    return self.file(env, start_response, file, mime, {**headers,
                        'Content-Security-Policy': "sandbox; default-src 'none'",
                        'Content-Disposition': f"{disposition}; filename*=UTF-8''{quote(name, safe='')}"}, reply, ranges=True)
                return reply(404, {'error': 'Unknown endpoint'})
            relative = 'index.html' if path == '/' else path.lstrip('/')
            if relative not in {'index.html','app.js','app.wasm','styles.css','archive.js','archive.css','bootstrap.js','connection.js'}:
                return reply(404, {'error': 'Not found'})
            file = safe_child(self.root / 'web', relative)
            if not file.is_file():
                return reply(404, {'error': 'Shell unavailable'})
            headers['Content-Security-Policy'] = "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            mime = 'application/wasm' if relative.endswith('.wasm') else mimetypes.guess_type(relative)[0] or 'application/octet-stream'
            return self.file(env, start_response, file, mime, headers, reply)
        except (ValueError, TypeError, AttributeError):
            return reply(400, {'error': 'Invalid request'})
        except (RuntimeError, OSError, sqlite3.Error, subprocess.TimeoutExpired):
            return reply(503, {'error': 'Archive unavailable'})

    @staticmethod
    def file(env, start_response, file, mime, headers, reply, ranges=False):
        length = file.stat().st_size
        try:
            start, end, code = byte_range(env.get('HTTP_RANGE') if ranges else None, length)
        except ValueError:
            return reply(416, {'error': 'Invalid range'}, {'Content-Range': f'bytes */{length}'})
        headers.update({'Content-Type': mime, 'Content-Length': str(end - start + 1)})
        if ranges:
            headers['Accept-Ranges'] = 'bytes'
        if code == 206:
            headers['Content-Range'] = f'bytes {start}-{end}/{length}'
        start_response(f'{code} {HTTPStatus(code).phrase}', list(headers.items()))
        if env['REQUEST_METHOD'] == 'HEAD':
            return []
        def stream():
            with file.open('rb') as source:
                source.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = source.read(min(128 * 1024, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    yield block
        return stream()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=r'D:\argusDB')
    parser.add_argument('--config', default='archive-api.json')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config = read_json(safe_child(root / 'private', args.config).read_bytes())
    start_database(root)
    import sys
    sys.path.insert(0, str(root / 'app/vendor/waitress-3.0.2-py3-none-any.whl'))
    from waitress import serve
    print('Argus ready on 127.0.0.1 only. No external access or collectors.', flush=True)
    serve(App(root, config, args.port), host='127.0.0.1', port=args.port, threads=6,
          connection_limit=64, channel_timeout=30, max_request_body_size=2048,
          clear_untrusted_proxy_headers=True, expose_tracebacks=False)

if __name__ == '__main__':
    main()
