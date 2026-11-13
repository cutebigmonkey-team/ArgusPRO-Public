"""Authenticated read-only archive API and shell, bound to IPv4 loopback only.

Not an internet server. Public hosting/reverse proxy support is deliberately a
separate deployment decision. No collector runs here.
"""
import argparse
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import subprocess
import threading
import time
from urllib.parse import parse_qs, quote, urlsplit

from storage import HEX, read_json, safe_child, start_database
from auth_store import AuthStore

PREFIX = '/api/archive'
COOKIE = 'argus_archive_session'
SESSION_SECONDS = 3600


def literal(value):
    # Hex encoding keeps untrusted search text out of SQL syntax and psql commands.
    return "convert_from(decode('" + value.encode('utf-8').hex() + "','hex'),'UTF8')"


class Archive:
    def __init__(self, root, config):
        self.root = Path(root).resolve()
        self.config = config
        self.media_root = safe_child(self.root, config.get('media_subdir', '.'))

    def query(self, sql):
        env = {**os.environ, **self.config['database'], 'PGCLIENTENCODING': 'UTF8',
               'PGCONNECT_TIMEOUT': '5', 'PGOPTIONS': '-c default_transaction_read_only=on -c statement_timeout=5000'}
        binary = self.root / 'tools' / 'pgsql' / 'bin' / 'psql.exe'
        command = str(binary) if binary.exists() else 'psql'
        result = subprocess.run([command, '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1'],
                                input=sql.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=12,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise RuntimeError('Archive database unavailable')
        return read_json(result.stdout)

    def materials(self, query):
        search = query.get('q', [''])[0]
        mode = query.get('sample', ['all'])[0]
        offset = int(query.get('offset', ['0'])[0])
        if len(search) > 200 or offset < 0 or offset > 1000000 or mode not in ('all', 'demo', 'production'):
            raise ValueError('Invalid filter')
        where = 'true'
        if search:
            where += f" AND strpos(lower(p.text_content || ' ' || p.platform || ' ' || p.canonical_key),lower({literal(search)}))>0"
        if mode != 'all':
            where += " AND COALESCE((k.manifest->>'sample')::boolean,false)=" + ('true' if mode == 'demo' else 'false')
        return self.query(f"""
WITH filtered AS (
 SELECT p.*, COALESCE((k.manifest->>'sample')::boolean,false) AS sample,k.imported_at
 FROM argus_archive.posts p JOIN argus_archive.packages k ON k.package_id=p.last_package_id
 WHERE {where}
), page AS (SELECT * FROM filtered ORDER BY observed_at DESC,canonical_key LIMIT 24 OFFSET {offset})
SELECT json_build_object(
 'total',(SELECT count(*) FROM filtered), 'offset',{offset}, 'limit',24,
 'packages',(SELECT count(*) FROM argus_archive.packages),
 'last_import',(SELECT max(imported_at) FROM argus_archive.packages),
 'items',COALESCE((SELECT json_agg(item ORDER BY observed_at DESC,canonical_key) FROM (
 SELECT observed_at,canonical_key,json_build_object(
  'id',p.canonical_key,'platform',p.platform,'url',p.post_url,'text',p.text_content,
  'published_at',p.published_at,'observed_at',p.observed_at,'imported_at',p.imported_at,
  'sample',p.sample,'tool',p.payload->>'tool',
  'files',COALESCE((SELECT json_agg(json_build_object('sha256',m.sha256,'size',m.size_bytes,
    'name',COALESCE((SELECT f->>'name' FROM jsonb_array_elements(
       CASE WHEN jsonb_typeof(p.payload->'evidence_files')='array' THEN p.payload->'evidence_files' ELSE '[]'::jsonb END) f
       WHERE f->>'sha256'=m.sha256 LIMIT 1),m.sha256)) ORDER BY m.sha256)
    FROM argus_archive.post_media pm JOIN argus_archive.media m USING(sha256)
    WHERE pm.canonical_key=p.canonical_key),'[]'::json)) AS item FROM page p
 ) rows),'[]'::json));""")

    def media(self, sha):
        if not HEX.fullmatch(sha):
            return None
        entry = self.query(f"""SELECT COALESCE((SELECT row_to_json(x) FROM (
 SELECT sha256,size_bytes FROM argus_archive.media m WHERE sha256='{sha}'
 AND EXISTS(SELECT 1 FROM argus_archive.post_media pm WHERE pm.sha256=m.sha256)
 ) x),'null'::json);""")
        if not entry:
            return None
        path = safe_child(self.media_root, f'media/{sha[:2]}/{sha}')
        if not path.is_file() or path.stat().st_size != entry['size_bytes']:
            raise RuntimeError('Stored attachment unavailable')
        return path


def media_type(path):
    with path.open('rb') as f:
        head = f.read(32)
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'image/webp'
    if head[4:8] == b'ftyp':
        return 'video/mp4'
    if head.lstrip(b'\xef\xbb\xbf').startswith(b'WEBVTT'):
        return 'text/vtt; charset=utf-8'
    return 'application/octet-stream'  # HTML/SVG/JS/JSON never executes on the API origin.


def byte_range(header, length):
    if not header:
        return 0, length - 1, 200
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not length or not any(match.groups()):
        raise ValueError('Invalid byte range')
    left, right = match.groups()
    if not left:
        if int(right) <= 0:
            raise ValueError('Invalid suffix range')
        start, end = max(0, length - int(right)), length - 1
    else:
        start, end = int(left), min(int(right), length - 1) if right else length - 1
    if start > end or start >= length:
        raise ValueError('Unsatisfiable byte range')
    return start, end, 206


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port, archive, web):
        self.archive, self.web = archive, Path(web).resolve()
        self.auth = AuthStore(safe_child(Path(archive.root) / 'private', archive.config.get('auth_file', 'users.sqlite3')))
        self.sessions = {}
        self.attempts = deque()
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(12)
        self.hash_slots = threading.BoundedSemaphore(2)
        super().__init__(('127.0.0.1', port), Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'
        self.cookie_name = COOKIE + '_' + str(self.server_port)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'ArgusArchive'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # No credentials, request bodies, private URLs or search text in logs.

    def response_headers(self, code, content_type, length, extra=None):
        self.send_response(code)
        for key, value in {'Content-Type': content_type, 'Content-Length': str(length),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                           'Cross-Origin-Resource-Policy': 'same-origin',
                           'Content-Security-Policy': "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                           **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()

    def json(self, code, value, extra=None):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.response_headers(code, 'application/json; charset=utf-8', len(data), extra)
        if self.command != 'HEAD':
            self.wfile.write(data)

    def allowed_origin(self, require=False):
        # Strict Host blocks DNS rebinding; browsers cannot choose a forged Host.
        if self.headers_in.get('Host') != urlsplit(self.server.origin).netloc:
            self.json(403, {'error': 'Untrusted host'})
            return False
        origin = self.headers_in.get('Origin')
        if (origin and origin != self.server.origin) or (require and origin != self.server.origin):
            self.json(403, {'error': 'Untrusted origin'})
            return False
        return True

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers_in.get('Cookie', ''))
            token = cookie[self.server.cookie_name].value if self.server.cookie_name in cookie else ''
        except Exception:
            return False
        with self.server.lock:
            expired = [k for k, record in self.server.sessions.items() if record['expires'] <= time.time()]
            for k in expired:
                del self.server.sessions[k]
            record = self.server.sessions.get(token)
        if record and self.server.auth.active(record):
            self.identity = record['username']
            return True
        return False

    def do_POST(self):
        self.headers_in = self.headers
        if not self.allowed_origin(require=True):
            return
        try:
            path = urlsplit(self.path).path
            if path == PREFIX + '/login':
                with self.server.lock:
                    now = time.monotonic()
                    while self.server.attempts and self.server.attempts[0] < now - 60:
                        self.server.attempts.popleft()
                    if len(self.server.attempts) >= 20:
                        self.json(429, {'error': 'Try again in one minute'})
                        return
                    self.server.attempts.append(now)
                length = int(self.headers_in.get('Content-Length', '0'))
                if not 0 < length <= 2048 or self.headers_in.get('Content-Type') != 'application/json':
                    self.json(400, {'error': 'Expected small JSON request'})
                    return
                data = read_json(self.rfile.read(length))
                if not self.server.hash_slots.acquire(blocking=False):
                    self.json(429, {'error': 'Try again shortly'})
                    return
                try:
                    identity = self.server.auth.authenticate(data.get('username', ''), data.get('key', ''))
                finally:
                    self.server.hash_slots.release()
                if not identity:
                    self.json(401, {'error': 'Invalid login or key'})
                    return
                session = secrets.token_urlsafe(32)
                with self.server.lock:
                    if len(self.server.sessions) >= 32:
                        self.server.sessions.pop(next(iter(self.server.sessions)))
                    self.server.sessions[session] = {**identity, 'expires': time.time() + SESSION_SECONDS}
                self.json(200, {'authenticated': True, 'username': identity['username']}, {'Set-Cookie': f'{self.server.cookie_name}={session}; HttpOnly; SameSite=Strict; Path={PREFIX}; Max-Age={SESSION_SECONDS}'})
                return
            if path == PREFIX + '/logout':
                cookie = SimpleCookie(self.headers_in.get('Cookie', ''))
                with self.server.lock:
                    if self.server.cookie_name in cookie:
                        self.server.sessions.pop(cookie[self.server.cookie_name].value, None)
                self.json(200, {'authenticated': False}, {'Set-Cookie': f'{self.server.cookie_name}=; HttpOnly; SameSite=Strict; Path={PREFIX}; Max-Age=0'})
                return
            self.json(405, {'error': 'Read-only API'})
        except (ValueError, TypeError, AttributeError):
            self.json(400, {'error': 'Invalid request'})

    def do_OPTIONS(self):
        self.headers_in = self.headers
        self.json(403, {'error': 'Cross-origin access is not enabled'})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.headers_in = self.headers
        if not self.allowed_origin():
            return
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        try:
            if path.startswith(PREFIX):
                if not self.session():
                    self.json(401, {'error': 'Authentication required'})
                    return
                if path == PREFIX + '/session':
                    self.json(200, {'authenticated': True, 'username': self.identity, 'archive_label': self.server.archive.config.get('label', 'Локальний архів')})
                elif path == PREFIX + '/materials':
                    self.json(200, self.server.archive.materials(query))
                elif path.startswith(PREFIX + '/media/'):
                    file = self.server.archive.media(path[len(PREFIX + '/media/'):])
                    if not file:
                        self.json(404, {'error': 'Attachment not found'})
                        return
                    name = query.get('name', [file.name])[0].replace('\\', '/').split('/')[-1]
                    name = ''.join(c for c in name if ord(c) >= 32 and ord(c) != 127)[:160] or file.name
                    mime = media_type(file)
                    disposition = 'attachment' if mime == 'application/octet-stream' or 'download' in query else 'inline'
                    self.file(file, mime, {'Content-Disposition': f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
                                         'Content-Security-Policy': "sandbox; default-src 'none'"}, ranges=True)
                else:
                    self.json(404, {'error': 'Unknown endpoint'})
                return
            # Explicit shell allowlist. Never expose the storage root via static serving.
            relative = 'index.html' if path == '/' else path.lstrip('/')
            names = {'index.html', 'app.js', 'app.wasm', 'styles.css', 'archive.js', 'archive.css', 'bootstrap.js'}
            if relative not in names:
                self.json(404, {'error': 'Not found'})
                return
            file = safe_child(self.server.web, relative)
            if not file.is_file():
                self.json(404, {'error': 'Shell has not been built'})
                return
            self.file(file, 'application/wasm' if relative.endswith('.wasm') else mimetypes.guess_type(relative)[0] or 'application/octet-stream')
        except (ValueError, TypeError):
            self.json(400, {'error': 'Invalid query'})
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            self.json(503, {'error': 'Local archive temporarily unavailable'})

    def file(self, path, mime, extra=None, ranges=False):
        length = path.stat().st_size
        try:
            start, end, code = byte_range(self.headers_in.get('Range') if ranges else None, length)
        except ValueError:
            self.json(416, {'error': 'Invalid range'}, {'Content-Range': f'bytes */{length}'})
            return
        headers = dict(extra or {})
        if ranges:
            headers['Accept-Ranges'] = 'bytes'
        if code == 206:
            headers['Content-Range'] = f'bytes {start}-{end}/{length}'
        self.response_headers(code, mime, end - start + 1, headers)
        if self.command == 'HEAD':
            return
        try:
            with path.open('rb') as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = f.read(min(1024 * 128, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=r'D:\argusDB')
    parser.add_argument('--config', default='archive-api.json')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    config = read_json(safe_child(root / 'private', args.config).read_bytes())
    start_database(root)
    server = Server(args.port, Archive(root, config), root / 'web')
    print(f'Argus archive: {server.origin}/#/archive', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
