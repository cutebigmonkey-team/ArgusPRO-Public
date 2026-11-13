"""Owner-authorized commands over the existing private Drive transport.

Google supplies the actor; a body cannot assign its own role. PostgreSQL owns
configuration, optimistic revisions, receipts and the audit log. No new service.
"""
import copy
import json
import os
import time
import io
import hashlib
from datetime import datetime, timezone

NAME = 'argus-control-command-v1.json'
FIELDS = 'id,name,description,version,parents,trashed,lastModifyingUser(permissionId)'


def number(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_range')
    return value


def validate_patch(patch, sources):
    if not isinstance(patch, dict) or not patch or set(patch) - {'sources', 'ai'}:
        raise ValueError('invalid_settings')
    result = copy.deepcopy(patch)
    if 'sources' in patch:
        if not isinstance(patch['sources'], dict) or len(patch['sources']) > 50:
            raise ValueError('invalid_sources')
        for sid, row in patch['sources'].items():
            if sid not in sources or not isinstance(row, dict) or set(row) - {'enabled', 'priority', 'interval', 'retry_limit', 'retry_delay'}:
                raise ValueError('invalid_source')
            for key, value in row.items():
                if key == 'enabled':
                    if type(value) is not bool: raise ValueError('invalid_enabled')
                else:
                    limits = {'priority': (-10, 10), 'interval': (60, 86400), 'retry_limit': (0, 20), 'retry_delay': (60, 86400)}
                    number(value, *limits[key])
    if 'ai' in patch:
        ai = patch['ai']
        if not isinstance(ai, dict) or set(ai) - {'enabled', 'concurrency', 'max_posts', 'interval', 'model'}:
            raise ValueError('unsupported_ai_option')
        for key, value in ai.items():
            if key == 'model':
                if value not in ('jev-1.14.0','jev-1.13.0','jev-latest','jev-preview'):raise ValueError('unsupported_model')
            elif key == 'enabled':
                if type(value) is not bool: raise ValueError('invalid_enabled')
            else:
                number(value, *{'concurrency': (1, 2), 'max_posts': (1, 16), 'interval': (300, 86400)}[key])
    return result


class Control:
    def __init__(self, schedule):
        self.sql, self.transaction = schedule.execute, schedule.transaction
        self.unpublished = True
        self.sql('CREATE TABLE IF NOT EXISTS runtime_control (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL, value TEXT NOT NULL)')
        self.sql('''CREATE TABLE IF NOT EXISTS runtime_control_audit (
            id TEXT PRIMARY KEY, actor TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL, detail TEXT NOT NULL, revision INTEGER NOT NULL, patch TEXT NOT NULL)''')
        self.sql("INSERT INTO runtime_control VALUES(1,0,'{}') ON CONFLICT(id) DO NOTHING")
        self.sql('CREATE TABLE IF NOT EXISTS runtime_control_files (id INTEGER PRIMARY KEY, file_id TEXT NOT NULL)')

    def current(self):
        row = self.sql('SELECT revision,value FROM runtime_control WHERE id=1').fetchone()
        return row['revision'], json.loads(row['value'])

    def process(self, file, owners, sources, probe):
        # The immutable receipt prevents retries/restarts from reapplying a command.
        if self.sql('SELECT id FROM runtime_control_audit WHERE id=?', (file['id'],)).fetchone(): return
        actor = file.get('lastModifyingUser', {}).get('permissionId', '')
        revision, config = self.current()
        status, detail, patch = 'rejected', 'forbidden', {}
        reset_source=None
        if actor in owners:
            try:
                raw = file.get('description', '')
                if len(raw.encode()) > 16000: raise ValueError('command_too_large')
                command = json.loads(raw)
                if not isinstance(command, dict) or command.get('format') != 'argus-control-command-v1': raise ValueError('invalid_command')
                if set(command) - {'format','action','revision','patch'}: raise ValueError('unknown_field')
                if type(command.get('revision')) is not int or command['revision'] != revision: raise ValueError('revision_conflict')
                if command.get('action') == 'test_ai':
                    models=probe()  # Actual provider request; no generation or archive text.
                    config['available_models']=models if isinstance(models,list) else []
                    status, detail = 'applied', 'typesafe_api_reachable'
                elif command.get('action') == 'retry_source':
                    patch=validate_patch(command.get('patch'),sources)
                    if set(patch)!={'sources'} or len(patch['sources'])!=1 or next(iter(patch['sources'].values()))!={}:raise ValueError('invalid_retry')
                    reset_source=next(iter(patch['sources']))
                    revision+=1
                    status,detail='applied','retry_budget_reset_cooldown_preserved'
                elif command.get('action') == 'update':
                    patch = validate_patch(command.get('patch'), sources)
                    if 'model' in patch.get('ai',{}) and patch['ai']['model'] not in config.get('available_models',[]):
                        raise ValueError('model_not_verified_available')
                    for section, values in patch.items():
                        config.setdefault(section, {})
                        if section == 'sources':
                            for sid, fields in values.items():config[section].setdefault(sid, {}).update(fields)
                        else:config[section].update(values)
                    revision += 1
                    status, detail = 'applied', 'configuration_committed'
                else:raise ValueError('invalid_action')
            except (ValueError, TypeError, KeyError):
                status, detail, patch = 'rejected', 'invalid_or_conflicting_command', {}
            except Exception:
                status, detail = 'failed', 'provider_unavailable'
        with self.transaction():
            if status == 'applied':
                if reset_source:self.sql('UPDATE runtime_schedule SET failures=0 WHERE id=?',(reset_source,))
                self.sql('UPDATE runtime_control SET revision=?,value=? WHERE id=1', (revision,json.dumps(config)))
            self.sql('INSERT INTO runtime_control_audit VALUES(?,?,?,?,?,?,?)',
                     (file['id'],actor,time.time(),status,detail,revision,json.dumps(patch)))
        self.unpublished = True

    def sync(self, store, catalog):
        files = store.service.files()
        folder = files.get(fileId=store.root,fields='owners(permissionId),trashed').execute(num_retries=3)
        owners = {o['permissionId'] for o in folder.get('owners', [])}
        if folder.get('trashed') or not owners:raise ValueError('archive_owner_unavailable')
        # Page through all commands. Changed metadata is fetched once as one atomic
        # Drive resource: description and Google-authenticated last modifier match.
        cursor = None
        while True:
            page = files.list(q=f"'{store.root}' in parents and trashed=false and name='{NAME}'",
                fields='files(id),nextPageToken',pageSize=100,pageToken=cursor,orderBy='createdTime').execute(num_retries=3)
            for entry in page.get('files', []):
                if self.sql('SELECT id FROM runtime_control_audit WHERE id=?',(entry['id'],)).fetchone():continue
                file = files.get(fileId=entry['id'],fields=FIELDS).execute(num_retries=3)
                if file.get('trashed') or file.get('name') != NAME or store.root not in file.get('parents', []):continue
                self.process(file,owners,{s['id'] for s in catalog['sources']},self.probe)
            cursor = page.get('nextPageToken')
            if not cursor:break
        return self.current()

    @staticmethod
    def probe():
        from typesafe_schedule import worker_class
        _, Client = worker_class()
        result = Client().call()
        if not isinstance(result, (list,dict)):raise RuntimeError('invalid_models_response')
        rows=result if isinstance(result,list) else result.get('models',[])
        if not isinstance(rows,list):raise RuntimeError('invalid_models_response')
        return [name for r in rows if (name:=(r if isinstance(r,str) else r.get('name',r.get('id',r.get('model'))) if isinstance(r,dict) else None)) in ('jev-1.14.0','jev-1.13.0','jev-latest','jev-preview')]

    def publish(self, store, schedule, registry):
        # Diagnostics/audit are an owner-only document, not an object inheriting
        # the archive's reader/editor grants. The shared locator contains only ID.
        files=store.service.files()
        owner=store.service.about().get(fields='user(permissionId)').execute(num_retries=3)['user']['permissionId']
        folder=files.get(fileId=store.root,fields='owners(permissionId)').execute(num_retries=3)
        if owner not in {r['permissionId'] for r in folder.get('owners',[])}:
            raise ValueError('control_requires_archive_owner_worker')
        revision, config = self.current()
        audit = [dict(r) for r in self.sql('SELECT id,actor,at,status,detail,revision FROM runtime_control_audit ORDER BY at DESC LIMIT 50').fetchall()]
        sources = []
        for row in self.sql('SELECT id,source,health,enabled,priority,next_check_at,failures FROM runtime_schedule').fetchall():
            source, health = json.loads(row['source']), json.loads(row['health'])
            sources.append({'id':row['id'],'name':source.get('name',row['id']),'platform':source['platform'],
                'enabled':bool(row['enabled']),'priority':row['priority'],'next_check_at':row['next_check_at'],
                'failures':row['failures'],'last_checked_at':health.get('last_checked_at'),
                'last_success_at':health.get('last_success_at'),'status':health.get('collector_status'),
                'interval':source.get('control_interval',300),'retry_limit':source.get('control_retry_limit',20),
                'retry_delay':source.get('control_retry_delay',60)})
        try:ai_config=json.loads(os.getenv('ARGUS_TYPESAFE_CONFIG','{}'))
        except ValueError:ai_config={}
        state={'format':'argus-control-state-v1','updated_at':datetime.now(timezone.utc).isoformat(),
            'revision':revision,'config':config,'audit':audit,'sources':sources,
            'platforms':[{'platform':r['platform'],'enabled':bool(r['enabled'])} for r in registry['collectors']],
            'ai':{'provider':'TypeSafe','model':config.get('ai',{}).get('model',ai_config.get('model','jev-1.14.0')),
                'available_models':config.get('available_models',[]),'configured':bool(os.getenv('TYPESAFE_API_KEY')),
                'enabled':config.get('ai',{}).get('enabled',os.getenv('ARGUS_TYPESAFE_ENABLED')=='true'),
                'concurrency':min(config.get('ai',{}).get('concurrency',2),2,int(ai_config.get('concurrency',2))),
                'max_posts':min(config.get('ai',{}).get('max_posts',4),4,int(ai_config.get('max_posts',4))),
                'interval':config.get('ai',{}).get('interval',300),
                'tasks':['analytics','reports'], 'generation_parameters':False},
            'database':{'status':'query_succeeded','checked_at':datetime.now(timezone.utc).isoformat()}}
        from googleapiclient.http import MediaIoBaseUpload
        row=self.sql('SELECT file_id FROM runtime_control_files WHERE id=1').fetchone()
        raw=json.dumps(state,ensure_ascii=False,separators=(',',':')).encode()
        media=MediaIoBaseUpload(io.BytesIO(raw),mimetype='application/json',resumable=False)
        if row:
            file_id=row['file_id']
            meta=files.get(fileId=file_id,fields='ownedByMe,shared,trashed').execute(num_retries=3)
            if not meta.get('ownedByMe') or meta.get('shared') or meta.get('trashed'):raise ValueError('control_state_not_private')
            saved=files.update(fileId=file_id,media_body=media,fields='id,md5Checksum').execute(num_retries=3)
        else:
            saved=files.create(body={'name':'argus-owner-control-state-v1.json'},media_body=media,fields='id,md5Checksum').execute(num_retries=0)
            with self.transaction():self.sql('INSERT INTO runtime_control_files VALUES(1,?)',(saved['id'],))
        if saved.get('md5Checksum')!=hashlib.md5(raw,usedforsecurity=False).hexdigest():raise ValueError('control_state_checksum')
        store.json('control-locator.json',{'format':'argus-control-locator-v1','id':saved['id']},mutable=True)
        self.unpublished = False
        return state


def apply_catalog(catalog, config):
    catalog = copy.deepcopy(catalog)
    for source in catalog['sources']:
        row = config.get('sources',{}).get(source['id'],{})
        for key in ('enabled','priority'):
            if key in row:source[key]=row[key]
        for key in ('interval','retry_limit','retry_delay'):
            if key in row:source['control_'+key]=row[key]
    return catalog
