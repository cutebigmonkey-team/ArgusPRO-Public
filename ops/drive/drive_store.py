"""Private Drive object storage. No SQL database and no permission changes."""
import hashlib
import io
import json
import os
import re
import time

SCOPES = ['https://www.googleapis.com/auth/drive.file']
ID = re.compile(r'[A-Za-z0-9_-]{10,200}\Z')
MAX_JSON = 20 * 1024**2
PART_BYTES = 8 * 1024**2
MAX_PARTS = 16
PARTS_FORMAT = 'argus-drive-parts-v1'

def can_partition(name):
    return name in ('posts.json','incremental-pending.json') or bool(re.fullmatch(r'package-[0-9a-f]{64}\.json',name))

def encoded(value):
    # A single emoji can widen the entire dumps() string to four bytes/character.
    # Encode JSON tokens directly so large archives do not hold that temporary
    # Unicode copy alongside their objects and UTF-8 bytes on small workers.
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                              separators=(',', ':'), allow_nan=False)
    output = io.BytesIO()
    for token in encoder.iterencode(value):
        output.write(token.encode('utf-8'))
    return output.getvalue()

def valid_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError('Invalid Drive file ID')
    return value


def write_parts(store, raw):
    if not 0 < len(raw) <= PART_BYTES * MAX_PARTS:
        raise ValueError('Partitioned archive exceeds the supported size')
    parts = []
    for offset in range(0, len(raw), PART_BYTES):
        chunk = raw[offset:offset+PART_BYTES]
        sha = hashlib.sha256(chunk).hexdigest()
        file_id = store.put('part-'+sha+'.bin', chunk)
        parts.append({'id':file_id, 'sha256':sha, 'size':len(chunk)})
    return parts


def reuse_snapshot_parts(store, raw, previous_raw, descriptors):
    """Keep immutable byte ranges even when the JSON header or earlier rows grow."""
    # read_parts verified the referenced bytes before collection merged any posts.
    spans = []
    source_offset = 0
    offset = 0
    for part in descriptors:
        end = source_offset + part['size']
        chunk = previous_raw[source_offset:end]
        source_offset = end
        if hashlib.sha256(chunk).hexdigest() != part['sha256']:
            return write_parts(store, raw)
        pos = raw.find(chunk, offset)
        if pos < 0:
            continue
        if pos > offset:
            spans.append((offset, pos, None))
        offset = pos + len(chunk)
        spans.append((pos, offset, part))
    if offset < len(raw):
        spans.append((offset, len(raw), None))
    count = sum(1 if part else (b-a+PART_BYTES-1)//PART_BYTES for a,b,part in spans)
    if count > MAX_PARTS:
        return write_parts(store, raw)
    parts = []
    for a,b,part in spans:
        parts.extend([part] if part else write_parts(store,raw[a:b]))
    return parts


def read_parts(store, parts, expected_sha, *, item_consumer=None):
    import tempfile
    from stream_json import load
    if not isinstance(parts,list) or not 1 <= len(parts) <= MAX_PARTS:
        raise ValueError('Invalid archive parts')
    digest=hashlib.sha256()
    # Verify all immutable bytes before decoding any record. Scratch space is
    # disposable; Drive remains the sole authoritative archive.
    with tempfile.TemporaryFile(mode='w+b') as spool:
        for part in parts:
            if (not isinstance(part,dict) or not isinstance(part.get('sha256'),str)
                    or not re.fullmatch(r'[0-9a-f]{64}',part['sha256'])
                    or type(part.get('size')) is not int or not 1 <= part['size'] <= PART_BYTES):
                raise ValueError('Invalid archive part descriptor')
            valid_id(part.get('id'))
            chunk=store.read_bytes('part-'+part['sha256']+'.bin',part['size'])
            if chunk is None or len(chunk)!=part['size'] or hashlib.sha256(chunk).hexdigest()!=part['sha256']:
                raise ValueError('Archive part integrity check failed')
            digest.update(chunk);spool.write(chunk)
        if digest.hexdigest()!=expected_sha:raise ValueError('Stored snapshot integrity check failed')
        del chunk
        spool.seek(0)
        with io.TextIOWrapper(spool,encoding='utf-8') as text:
            return load(text,item_consumer=item_consumer)


def encoded_file(value,stream):
    """Canonical existing wire format, bounded by an individual JSON token."""
    encoder=json.JSONEncoder(ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    digest=hashlib.sha256();size=0
    for token in encoder.iterencode(value):
        block=token.encode('utf-8');stream.write(block);digest.update(block);size+=len(block)
    return digest.hexdigest(),size


def write_file_parts(store,stream,size):
    if not 0<size<=PART_BYTES*MAX_PARTS:raise ValueError('Partitioned archive exceeds the supported size')
    stream.seek(0);parts=[]
    while chunk:=stream.read(PART_BYTES):
        sha=hashlib.sha256(chunk).hexdigest();file_id=store.put('part-'+sha+'.bin',chunk)
        parts.append({'id':file_id,'sha256':sha,'size':len(chunk)})
    return parts

def service_from_info(info):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    # Never accept a token endpoint supplied in a credential file.
    required = ('client_id', 'client_secret', 'refresh_token')
    if any(not isinstance(info.get(k), str) or not info[k] for k in required):
        raise ValueError('Missing Drive OAuth configuration')
    credentials = Credentials(None, token_uri='https://oauth2.googleapis.com/token',
        client_id=info['client_id'], client_secret=info['client_secret'],
        refresh_token=info['refresh_token'], scopes=SCOPES)
    return build('drive', 'v3', credentials=credentials, cache_discovery=False)

class DriveStore:
    def __init__(self, service, root):
        self.service, self.root = service, valid_id(root)
        self.telemetry={'downloads':0,'download_bytes':0,'download_seconds':0.0,
                        'uploads':0,'upload_bytes':0,'upload_seconds':0.0}

    def assert_private(self):
        folder = self.service.files().get(fileId=self.root,
            fields='id,mimeType,trashed,capabilities(canAddChildren)').execute(num_retries=3)
        if folder.get('trashed') or folder.get('mimeType') != 'application/vnd.google-apps.folder':
            raise ValueError('Archive root is not an active folder')
        if not folder.get('capabilities', {}).get('canAddChildren'):
            raise ValueError('Archive folder is not writable')
        page = None
        while True:
            permissions = self.service.permissions().list(fileId=self.root,
                fields='nextPageToken,permissions(type,role)', pageToken=page).execute(num_retries=3)
            if any(p['type'] != 'user' for p in permissions.get('permissions', [])):
                raise ValueError('Archive must be private and shared only with individual accounts')
            page = permissions.get('nextPageToken')
            if not page: break

    def find(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}', name):
            raise ValueError('Invalid object name')
        result = self.service.files().list(q=f"'{self.root}' in parents and trashed=false and name='{name}'",
            fields='files(id,name,size,md5Checksum),nextPageToken', pageSize=2).execute(num_retries=3)
        files = result.get('files', [])
        if len(files) > 1 or result.get('nextPageToken'):
            raise ValueError('Duplicate archive objects require owner review')
        return files[0] if files else None

    def read_bytes(self, name, limit):
        started=time.monotonic()
        entry = self.find(name)
        if not entry: return None
        if int(entry.get('size', limit + 1)) > limit:
            raise ValueError('Archive JSON is too large')
        data = self.service.files().get_media(fileId=entry['id']).execute(num_retries=3)
        if len(data) > limit: raise ValueError('Archive JSON is too large')
        self.telemetry['downloads']+=1;self.telemetry['download_bytes']+=len(data)
        self.telemetry['download_seconds']+=time.monotonic()-started
        return data

    def read_json(self, name):
        data = self.read_bytes(name, MAX_JSON)
        if data is None:return None
        value = json.loads(data)
        if can_partition(name) and isinstance(value,dict) and value.get('format')==PARTS_FORMAT:
            return read_parts(self,value['parts'],value['sha256'])
        return value

    def put(self, name, data, mutable=False):
        """Single workflow writer. Retry-safe immutable names; pointer is committed last."""
        from googleapiclient.http import MediaIoBaseUpload
        started=time.monotonic()
        entry = self.find(name)
        expected = hashlib.md5(data, usedforsecurity=False).hexdigest()
        if entry and entry.get('md5Checksum') == expected and int(entry.get('size', -1)) == len(data):
            return entry['id']
        if entry and not mutable:
            raise ValueError('Existing immutable archive object has different content')
        mimetype = 'text/plain' if name.endswith('.txt') else ('application/json' if name.endswith('.json') else 'application/octet-stream')
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype,
                                 chunksize=4*1024**2, resumable=True)
        if entry:
            request = self.service.files().update(fileId=entry['id'],media_body=media,
                fields='id,size,md5Checksum')
        else:
            # A fixed ID makes retries within this upload safe, even after a lost response.
            file_id = self.service.files().generateIds(count=1,space='drive',type='files').execute(num_retries=3)['ids'][0]
            request = self.service.files().create(body={'id':file_id,'name':name,'parents':[self.root]},
                media_body=media,fields='id,size,md5Checksum')
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=3)
        if response.get('md5Checksum') != expected or int(response.get('size', -1)) != len(data):
            raise ValueError('Drive upload checksum or size mismatch')
        self.telemetry['uploads']+=1;self.telemetry['upload_bytes']+=len(data)
        self.telemetry['upload_seconds']+=time.monotonic()-started
        return response['id']

    def json(self, name, value, mutable=False):
        data = encoded(value)
        if can_partition(name) and len(data)>MAX_JSON:
            data=encoded({'format':PARTS_FORMAT,'parts':write_parts(self,data),'sha256':hashlib.sha256(data).hexdigest()})
        if len(data) > MAX_JSON: raise ValueError('Archive index exceeds 20 MiB; shard before expanding')
        return self.put(name, data, mutable)


def from_environment():
    info = json.loads(os.environ.get('ARGUS_DRIVE_OAUTH', '{}'))
    return DriveStore(service_from_info(info), os.environ.get('ARGUS_DRIVE_FOLDER_ID', ''))
