"""Provision a read-only DB role and individual local accounts; never prints secrets."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

from storage import read_json, start_database
from auth_store import AuthStore


def configure(root, test=False):
    root = Path(root).resolve()
    private = root / 'private'
    if os.name == 'nt':
        identity = subprocess.run(['whoami'], capture_output=True, check=True, text=True).stdout.strip()
        subprocess.run(['icacls', str(private), '/inheritance:r', '/grant:r',
                        identity + ':(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], check=True, stdout=subprocess.DEVNULL)
    start_database(root)
    admin = read_json((private / 'postgres-admin.json').read_bytes())
    path = private / ('archive-api-test.json' if test else 'archive-api.json')
    existing = read_json(path.read_bytes()) if path.exists() else None
    password = existing['database']['PGPASSWORD'] if existing else secrets.token_hex(32)
    auth_file = 'users-test.sqlite3' if test else 'users.sqlite3'
    AuthStore(private / auth_file)
    database = 'argus_storage_test' if test else 'argus'
    role = 'argus_api_test' if test else 'argus_api'
    # Database and role names are fixed constants. Generated passwords are hex only.
    if len(password) != 64 or any(c not in '0123456789abcdef' for c in password):
        raise ValueError('Invalid managed database password')
    sql = f"""
DO $$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN
  CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT PASSWORD '{password}';
 END IF;
END $$;
ALTER ROLE {role} SET default_transaction_read_only=on;
GRANT CONNECT ON DATABASE {database} TO {role};
GRANT USAGE ON SCHEMA argus_archive TO {role};
GRANT SELECT ON argus_archive.posts,argus_archive.packages,argus_archive.media,argus_archive.post_media TO {role};
"""
    env = {**os.environ, **admin, 'PGDATABASE': database, 'PGCLIENTENCODING': 'UTF8'}
    result = subprocess.run([str(root / 'tools/pgsql/bin/psql.exe'), '-X', '-q', '-w', '-v', 'ON_ERROR_STOP=1'],
                            input=sql.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
    if result.returncode:
        raise RuntimeError('Could not provision read-only API role')
    config = {'database': {**{k: admin[k] for k in ('PGHOST', 'PGPORT')}, 'PGDATABASE': database,
                           'PGUSER': role, 'PGPASSWORD': password},
              'auth_file': auth_file,
              'media_subdir': 'verification' if test else '.',
              'label': 'Тестовий архів · публічні приклади' if test else 'Мій архів Argus'}
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    if os.name != 'nt':
        path.chmod(0o600)
    print('API configured. Use Manage-Users.cmd to create individual logins and keys. Legacy shared keys are not accepted.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', default=r'D:\argusDB')
    parser.add_argument('--test', action='store_true')
    args = parser.parse_args()
    configure(args.root, args.test)
