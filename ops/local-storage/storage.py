"""On-demand, standard-library-only package import. No daemon or social-site requests."""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile

FORMAT = 'argus-package-v1'
HEX = re.compile(r'[0-9a-f]{64}\Z')
MAX_ARCHIVE = 2 * 1024**3
MAX_TOTAL = 4 * 1024**3
MAX_FILE = 512 * 1024**2
MAX_POSTS = 64 * 1024**2
MAX_ENTRIES = 10000


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(path):
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_json(data):
    return json.loads(data, object_pairs_hook=strict_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Non-finite JSON value')))


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Timestamp must be an ISO string')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    return parsed.astimezone(dt.timezone.utc).isoformat()


def safe_child(root, relative):
    root = Path(root).resolve()
    result = root / relative
    if not result.resolve().is_relative_to(root):
        raise ValueError('Storage path escapes its root')
    current = result
    while current != root:
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError('Links are not permitted inside storage paths')
        current = current.parent
    return result


@contextlib.contextmanager
def import_lock(root):
    path = safe_child(root, 'import.lock')
    with path.open('a+b') as handle:
        handle.seek(0)
        handle.write(b'0')
        handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def validate_package(archive, root):
    if Path(archive).stat().st_size > MAX_ARCHIVE:
        raise ValueError('Package exceeds the archive size limit')
    staging = safe_child(root, 'staging')
    staging.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='import-', dir=staging) as temp, zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > MAX_ENTRIES or len(set(names)) != len(names):
            raise ValueError('Too many entries or duplicate ZIP paths')
        if 'manifest.json' not in names or bundle.getinfo('manifest.json').file_size > 1024**2:
            raise ValueError('Missing or oversized manifest')
        for entry in entries:
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1:
                raise ValueError('Directory, symlink or encrypted ZIP entry')
            if entry.file_size > MAX_FILE:
                raise ValueError('ZIP entry exceeds size limit')
        if sum(entry.file_size for entry in entries) > MAX_TOTAL:
            raise ValueError('Expanded package exceeds size limit')
        manifest = read_json(bundle.read('manifest.json'))
        if not isinstance(manifest, dict) or set(manifest) != {'format', 'package_id', 'created_at', 'files', 'sample'}:
            raise ValueError('Unsupported manifest structure')
        if manifest['format'] != FORMAT or type(manifest['sample']) is not bool:
            raise ValueError('Unsupported package format')
        timestamp(manifest['created_at'])
        identity = {k: v for k, v in manifest.items() if k != 'package_id'}
        if manifest['package_id'] != hashlib.sha256(canonical(identity)).hexdigest():
            raise ValueError('Package ID does not match its manifest')
        files = manifest['files']
        if not isinstance(files, list) or len(files) >= MAX_ENTRIES:
            raise ValueError('Invalid file inventory')
        inventory = {}
        for item in files:
            if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
                raise ValueError('Invalid file descriptor')
            name = item['path']
            if not isinstance(name, str) or not (name == 'posts.jsonl' or re.fullmatch(r'media/[0-9a-f]{64}', name)):
                raise ValueError('Unexpected or unsafe package path')
            if name in inventory or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE:
                raise ValueError('Duplicate file or invalid size')
            if not isinstance(item['sha256'], str) or not HEX.fullmatch(item['sha256']):
                raise ValueError('Invalid SHA-256')
            if name.startswith('media/') and name[6:] != item['sha256']:
                raise ValueError('Media filename does not match its content hash')
            inventory[name] = item
        if 'posts.jsonl' not in inventory or set(names) != set(inventory) | {'manifest.json'}:
            raise ValueError('ZIP content differs from the manifest')
        if inventory['posts.jsonl']['size'] > MAX_POSTS:
            raise ValueError('Post list exceeds size limit')
        for name, item in inventory.items():
            if bundle.getinfo(name).file_size != item['size']:
                raise ValueError('File size mismatch')
            destination = Path(temp) / name
            destination.parent.mkdir(exist_ok=True)
            count = 0
            checksum = hashlib.sha256()
            with bundle.open(name) as source, destination.open('wb') as target:
                while block := source.read(1024**2):
                    count += len(block)
                    if count > item['size']:
                        raise ValueError('Expanded file exceeds declared size')
                    checksum.update(block)
                    target.write(block)
            if count != item['size'] or checksum.hexdigest() != item['sha256']:
                raise ValueError('SHA-256 integrity check failed')
        posts = []
        keys = set()
        with (Path(temp) / 'posts.jsonl').open(encoding='utf-8') as source:
            for line in source:
                post = read_json(line)
                if not isinstance(post, dict):
                    raise ValueError('Post must be an object')
                for field in ('canonical_key', 'platform', 'post_url', 'text', 'observed_at'):
                    if not isinstance(post.get(field), str) or '\0' in post[field]:
                        raise ValueError('Invalid post field: ' + field)
                key = post['canonical_key']
                if not key or len(key) > 2048 or key in keys or not post['platform']:
                    raise ValueError('Missing or duplicate canonical post identity')
                keys.add(key)
                if urllib.parse.urlsplit(post['post_url']).scheme not in ('https', 'http'):
                    raise ValueError('Post URL must be HTTP(S)')
                post['observed_at'] = timestamp(post['observed_at'])
                post['published_at'] = timestamp(post['published_at']) if post.get('published_at') else None
                media = post.get('media', [])
                if not isinstance(media, list) or any(not isinstance(s, str) or 'media/' + s not in inventory for s in media):
                    raise ValueError('Post references absent media')
                post['media'] = sorted(set(media))
                posts.append(post)
                if len(posts) > 100000:
                    raise ValueError('Too many posts')
        yield manifest, posts, Path(temp)


def database_env(root):
    env = os.environ.copy()
    config = Path(root) / 'private' / 'postgres.json'
    if config.exists():
        env.update({str(k): str(v) for k, v in read_json(config.read_bytes()).items()})
    env['PGCLIENTENCODING'] = 'UTF8'
    env['PGCONNECT_TIMEOUT'] = '10'
    return env


def psql(root, sql):
    executable = Path(root) / 'tools' / 'pgsql' / 'bin' / 'psql.exe'
    command = str(executable) if executable.exists() else shutil.which('psql')
    if not command:
        raise RuntimeError('PostgreSQL client is not installed')
    result = subprocess.run([command, '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1'],
                            input=sql.encode('utf-8'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=database_env(root), timeout=300)
    if result.returncode:
        # Connection errors can echo private connection details. Keep them out of logs.
        raise RuntimeError('PostgreSQL operation failed; transaction was not committed')
    return result.stdout.decode('utf-8').strip()


def start_database(root):
    control = Path(root) / 'tools' / 'pgsql' / 'bin' / 'pg_ctl.exe'
    data = Path(root) / 'postgres'
    if not control.exists() or not (data / 'PG_VERSION').exists():
        return  # CI and existing PostgreSQL installations use their own server.
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    status = subprocess.run([str(control), '-D', str(data), 'status'], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, creationflags=flags)
    if status.returncode == 0:
        return
    result = subprocess.run([str(control), '-D', str(data), '-l', str(Path(root) / 'logs' / 'postgres.log'),
                             '-w', '-t', '60', 'start'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=90, creationflags=flags)
    if result.returncode:
        raise RuntimeError('Local PostgreSQL could not start; see logs/postgres.log')


def json_sql(value):
    encoded = base64.b64encode(canonical(value)).decode('ascii')
    return "convert_from(decode('" + encoded + "','base64'),'UTF8')::jsonb"


def import_package(archive, root, allow_sample=False):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with import_lock(root), validate_package(archive, root) as (manifest, posts, staging):
        if manifest['sample'] and not allow_sample:
            raise ValueError('Sample package requires --allow-sample; no real collection data in this package')
        media = []
        # Publish immutable content before the DB transaction. A crash can leave reusable,
        # unreferenced blobs, but can never commit rows pointing at missing media.
        for item in manifest['files']:
            if not item['path'].startswith('media/'):
                continue
            sha = item['sha256']
            relative = f'media/{sha[:2]}/{sha}'
            target = safe_child(root, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.stat().st_size != item['size'] or digest(target) != sha:
                    raise ValueError('Existing local media is corrupted')
            else:
                os.replace(staging / item['path'], target)
            media.append({'sha256': sha, 'size': item['size'], 'path': relative})
        schema = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
        package_id = manifest['package_id']  # Verified 64-character digest, never untrusted SQL.
        sql = "BEGIN; SET LOCAL statement_timeout='120s'; SELECT pg_advisory_xact_lock(782343901);\n" + schema
        sql += f"""
CREATE TEMP TABLE _incoming (payload jsonb) ON COMMIT DROP;
COPY _incoming(payload) FROM STDIN;
"""
        for post in posts:
            # COPY text protocol escaping is separate from JSON escaping.
            sql += canonical(post).decode('utf-8').replace('\\', '\\\\').replace('\t', '\\t').replace('\r', '\\r').replace('\n', '\\n') + '\n'
        sql += '\\.\n'
        sql += f"""
CREATE TEMP TABLE _claimed ON COMMIT DROP AS
WITH added AS (INSERT INTO argus_archive.packages(package_id,manifest)
 VALUES ('{package_id}',{json_sql(manifest)}) ON CONFLICT DO NOTHING RETURNING package_id)
SELECT * FROM added;
INSERT INTO argus_archive.media(sha256,size_bytes,relative_path)
 SELECT m->>'sha256',(m->>'size')::bigint,m->>'path'
 FROM jsonb_array_elements({json_sql(media)}) m WHERE EXISTS(SELECT 1 FROM _claimed)
 ON CONFLICT(sha256) DO NOTHING;
INSERT INTO argus_archive.posts(canonical_key,platform,post_url,text_content,published_at,observed_at,payload,last_package_id)
 SELECT payload->>'canonical_key',payload->>'platform',payload->>'post_url',payload->>'text',
 (payload->>'published_at')::timestamptz,(payload->>'observed_at')::timestamptz,payload,'{package_id}'
 FROM _incoming WHERE EXISTS(SELECT 1 FROM _claimed)
 ON CONFLICT(canonical_key) DO UPDATE SET
 platform=EXCLUDED.platform,post_url=EXCLUDED.post_url,text_content=EXCLUDED.text_content,
 published_at=EXCLUDED.published_at,observed_at=EXCLUDED.observed_at,
 payload=EXCLUDED.payload,last_package_id=EXCLUDED.last_package_id
 WHERE EXCLUDED.observed_at > argus_archive.posts.observed_at;
INSERT INTO argus_archive.post_media(canonical_key,sha256)
 SELECT payload->>'canonical_key',m FROM _incoming,
 LATERAL jsonb_array_elements_text(payload->'media') m WHERE EXISTS(SELECT 1 FROM _claimed)
 ON CONFLICT DO NOTHING;
SELECT json_build_object('status',CASE WHEN EXISTS(SELECT 1 FROM _claimed) THEN 'imported' ELSE 'already_imported' END,
 'package_id','{package_id}','posts_in_package',{len(posts)},'media_in_package',{len(media)},
 'sample',{'true' if manifest['sample'] else 'false'});
COMMIT;
"""
        output = psql(root, sql)
        result = read_json(output.splitlines()[-1])
        receipts = safe_child(root, 'receipts')
        receipts.mkdir(exist_ok=True)
        receipt = receipts / (package_id + '.json')
        temp_receipt = receipt.with_suffix('.tmp')
        temp_receipt.write_bytes(canonical(result))
        os.replace(temp_receipt, receipt)
        return result


def pack(posts_path, media_dir, output, sample=False, *, created_at=None):
    posts_bytes = Path(posts_path).read_bytes()
    data = {'posts.jsonl': posts_bytes}
    for path in sorted(Path(media_dir).iterdir()):
        if not path.is_file() or path.is_symlink():
            raise ValueError('Media directory must contain regular files only')
        blob = path.read_bytes()
        data['media/' + hashlib.sha256(blob).hexdigest()] = blob
    manifest = {'format': FORMAT, 'created_at': created_at or dt.datetime.now(dt.timezone.utc).isoformat(), 'sample': sample,
                'files': [{'path': name, 'size': len(blob), 'sha256': hashlib.sha256(blob).hexdigest()}
                          for name, blob in sorted(data.items())]}
    manifest['package_id'] = hashlib.sha256(canonical(manifest)).hexdigest()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.partial')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('manifest.json', canonical(manifest))
        for name, blob in data.items():
            bundle.writestr(name, blob)
    with tempfile.TemporaryDirectory() as root:
        with validate_package(temporary, root):
            pass
    os.replace(temporary, output)
    return manifest['package_id']


def github_token():
    token = os.environ.get('ARGUS_GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        return token
    git = shutil.which('git')
    if git:
        result = subprocess.run([git, 'credential', 'fill'], input=b'protocol=https\nhost=github.com\nusername=cutebigmonkey\n\n',
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never'})
        values = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
        if values.get('password'):
            return values['password']
    raise RuntimeError('GitHub sign-in required: use Git Credential Manager or ARGUS_GITHUB_TOKEN for this run')


class ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or not parsed.hostname or not (
            parsed.hostname == 'api.github.com' or parsed.hostname.endswith('.blob.core.windows.net') or
            parsed.hostname.endswith('.githubusercontent.com') or parsed.hostname.endswith('.github.com')):
            raise ValueError('Unexpected artifact redirect host')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if parsed.hostname != 'api.github.com':
            redirected.remove_header('Authorization')
        return redirected


def download_latest(root, repo, workflow, artifact_name, branch='backend_argusPRO'):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid repository name')
    opener = urllib.request.build_opener(ArtifactRedirect())
    headers = {'Authorization': 'Bearer ' + github_token(), 'Accept': 'application/vnd.github+json',
               'User-Agent': 'Argus-on-demand-import', 'X-GitHub-Api-Version': '2022-11-28'}
    base = 'https://api.github.com/repos/' + repo
    def api(path):
        with opener.open(urllib.request.Request(base + path, headers=headers), timeout=45) as response:
            return read_json(response.read(8 * 1024**2))
    runs = api('/actions/workflows/' + urllib.parse.quote(workflow, safe='') + '/runs?branch=' + urllib.parse.quote(branch, safe='') + '&status=success&per_page=20')
    selected = None
    for run in runs.get('workflow_runs', []):
        artifacts = api(f"/actions/runs/{int(run['id'])}/artifacts?per_page=100")
        selected = next((a for a in artifacts.get('artifacts', []) if a['name'] == artifact_name and not a['expired']), None)
        if selected:
            break
    if not selected:
        raise RuntimeError('No successful package export found. Collection/export must create the configured artifact first.')
    expected = selected.get('digest', '')
    if not expected.startswith('sha256:') or not HEX.fullmatch(expected[7:]):
        raise ValueError('GitHub artifact has no verifiable SHA-256 digest')
    inbox = safe_child(root, 'inbox')
    inbox.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='download-', dir=inbox) as temp:
        outer = Path(temp) / 'artifact.zip'
        request = urllib.request.Request(base + f"/actions/artifacts/{int(selected['id'])}/zip", headers=headers)
        with opener.open(request, timeout=90) as source, outer.open('wb') as target:
            total = 0
            while block := source.read(1024**2):
                total += len(block)
                if total > MAX_ARCHIVE:
                    raise ValueError('Artifact exceeds size limit')
                target.write(block)
        if digest(outer) != expected[7:]:
            raise ValueError('GitHub artifact checksum mismatch')
        with zipfile.ZipFile(outer) as bundle:
            if bundle.namelist() != ['package.zip'] or bundle.getinfo('package.zip').file_size > MAX_ARCHIVE:
                raise ValueError('Artifact must contain exactly package.zip')
            inner = Path(temp) / 'package.zip'
            with bundle.open('package.zip') as source, inner.open('wb') as target:
                shutil.copyfileobj(source, target, 1024**2)
        destination = inbox / (str(int(selected['id'])) + '.zip')
        os.replace(inner, destination)
        return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default=r'D:\argusDB')
    commands = parser.add_subparsers(dest='command', required=True)
    imp = commands.add_parser('import')
    imp.add_argument('archive')
    imp.add_argument('--allow-sample', action='store_true')
    receive = commands.add_parser('receive')
    receive.add_argument('--repo', default='cutebigmonkey-team/ArgusPRO')
    receive.add_argument('--workflow', default='storage-package.yml')
    receive.add_argument('--artifact', default='argus-package')
    receive.add_argument('--branch', default='backend_argusPRO')
    receive.add_argument('--allow-sample', action='store_true')
    package = commands.add_parser('pack')
    package.add_argument('--posts', required=True)
    package.add_argument('--media', required=True)
    package.add_argument('--output', required=True)
    package.add_argument('--sample', action='store_true')
    args = parser.parse_args()
    if args.command == 'pack':
        print(pack(args.posts, args.media, args.output, args.sample))
        return
    archive = args.archive if args.command == 'import' else download_latest(args.root, args.repo, args.workflow, args.artifact, args.branch)
    start_database(args.root)
    print(json.dumps(import_package(archive, args.root, args.allow_sample), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        if isinstance(error, (ValueError, RuntimeError)):
            print('Import failed: ' + str(error), file=sys.stderr)
        else:
            print('Import failed (' + type(error).__name__ + '); no successful import reported.', file=sys.stderr)
        sys.exit(1)
