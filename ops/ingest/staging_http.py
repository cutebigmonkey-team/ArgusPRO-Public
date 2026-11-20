"""Explicitly disabled-by-default, LOOPBACK-ONLY integration fixture gateway.

This is NOT a production ingress implementation. It intentionally uses a
separate disposable/staging PostgreSQL database, never the Hostless database.
It does not create schemas, apply migrations, run a worker, or touch Drive.
"""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, unquote
from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler

ALLOWED_DB_NAMES = frozenset({'argus_ingest_ci', 'argus_ingest_staging'})
ALLOWED_ROLES = frozenset({'argus_staging_ingress', 'argus_ci'})
ALLOWED_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def validate_staging_config(env):
    """Never let this executable be mistaken for a production service."""
    if env.get('ARGUS_STAGING_GATEWAY_ENABLED') != '1':
        raise ValueError('staging_gateway_disabled')
    if env.get('ARGUS_ARCHIVE_WRITER_OWNER'):
        raise ValueError('writer_environment_forbidden')
    if env.get('ARGUS_DRIVE_OAUTH') or env.get('ARGUS_JSONBIN_KEY'):
        raise ValueError('archive_credentials_forbidden')
    dsn = env.get('ARGUS_STAGING_PG_DSN', '')
    if not dsn or any(ord(c) < 32 for c in dsn):
        raise ValueError('staging_dsn_required')
    # Only accept a simple PostgreSQL URI. Reject libpq service files, hidden
    # hostaddr overrides and query-string overrides of host/user/dbname.
    try:
        uri = urlsplit(dsn)
        if uri.scheme not in ('postgresql', 'postgres') or uri.query or uri.fragment:
            raise ValueError('invalid_staging_dsn')
        dbname = unquote(uri.path.lstrip('/'))
        user = unquote(uri.username or '')
        host = uri.hostname
        if not dbname or '/' in dbname or not host or not user or uri.password is not None:
            raise ValueError('invalid_staging_dsn')
        if uri.port is not None and not 1 <= uri.port <= 65535:
            raise ValueError('invalid_staging_dsn')
    except (ValueError, TypeError):
        raise ValueError('invalid_staging_dsn') from None
    if dbname not in ALLOWED_DB_NAMES or user not in ALLOWED_ROLES:
        raise ValueError('staging_database_or_role_required')
    if host not in ALLOWED_HOSTS:
        raise ValueError('staging_loopback_required')
    parsed = {'dbname': dbname, 'user': user, 'host': host}
    path = env.get('ARGUS_STAGING_SOURCE_POLICY', '')
    if not path or not Path(path).is_absolute():
        raise ValueError('private_source_policy_required')
    for key in ('ARGUS_STAGING_REPOSITORY_ID', 'ARGUS_STAGING_OWNER_ID'):
        if not re.fullmatch(r'[1-9][0-9]{0,19}', env.get(key, '')):
            raise ValueError('staging_identity_required')
    return parsed


def load_source_policy(path):
    """Private registry snapshot: IDs and platforms, not URLs or credentials."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'sources'} or not isinstance(data['sources'], list):
        raise ValueError('invalid_private_source_policy')
    allowed = {}
    for item in data['sources']:
        if (not isinstance(item, dict) or set(item) != {'id', 'platform'}
                or not isinstance(item['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{3,100}', item['id'])
                or not isinstance(item['platform'], str) or not re.fullmatch(r'[a-z0-9_]{2,40}', item['platform'])
                or item['id'] in allowed):
            raise ValueError('invalid_private_source_policy')
        allowed[item['id']] = item['platform']
    if not allowed:
        raise ValueError('empty_staging_source_policy')

    def authorize(part, identity):
        if 'recover_source_id' in part:
            return False  # Staging HTTP has no cursor-recovery endpoint.
        source = part.get('source', {})
        return allowed.get(source.get('source_id')) == source.get('platform')
    return authorize


def connect_staging(env):
    """Return dedicated autocommit connection; never create/migrate a schema."""
    validate_staging_config(env)
    import psycopg
    conn = psycopg.connect(env['ARGUS_STAGING_PG_DSN'], autocommit=True, connect_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT current_database(), current_user, inet_server_addr()')
            db, role, address = cur.fetchone()
            if db not in ALLOWED_DB_NAMES or role not in ALLOWED_ROLES:
                raise ValueError('unexpected_staging_database_or_role')
            if address is not None and str(address) not in ('127.0.0.1', '::1'):
                raise ValueError('unexpected_staging_server_address')
            cur.execute("SELECT to_regclass('argus_ingest.batches') IS NOT NULL")
            if cur.fetchone()[0] is not True:
                raise ValueError('staging_schema_not_initialized')
        return conn
    except Exception:
        conn.close()
        raise


def build_gateway(env, conn):
    from private_gateway import PrivateGateway
    from pg_queue import PostgresQueue
    policy = load_source_policy(env['ARGUS_STAGING_SOURCE_POLICY'])
    return PrivateGateway(PostgresQueue(conn), policy,
                          repository_id=env['ARGUS_STAGING_REPOSITORY_ID'],
                          owner_id=env['ARGUS_STAGING_OWNER_ID'], enabled=True)


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        # Never log untrusted request headers, paths or payloads.
        return


def serve(env=None, *, server_factory=make_server):
    env = dict(os.environ if env is None else env)
    validate_staging_config(env)
    conn = connect_staging(env)
    gateway = build_gateway(env, conn)
    try:
        # Always binds loopback; never accepts remote connections.
        with server_factory('127.0.0.1', 8768, gateway.http, handler_class=QuietHandler) as server:
            print('ARGUS_STAGING_INGEST_LOOPBACK_ONLY', flush=True)
            server.serve_forever()
    finally:
        conn.close()


if __name__ == '__main__':
    serve()
