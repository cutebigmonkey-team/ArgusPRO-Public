"""One read-only legacy catalog export. No posts, credentials or DB mutations."""
import collections
import json
import os
from pathlib import Path
import psycopg

def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('GitHub-hosted runner required')
    target = Path('collection/sources.json'); target.parent.mkdir(exist_ok=True)
    dsn = os.environ.get('ARGUS_LEGACY_CATALOG_DSN')
    if not dsn:
        raise RuntimeError('Legacy catalog credential missing')
    with psycopg.connect(dsn, connect_timeout=20, sslmode='require') as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        conn.execute("SET LOCAL statement_timeout='20s'")
        rows = conn.execute('SELECT s.id::text,s.name,s.url,p.key,s.origin,s.enabled FROM sources s JOIN platforms p ON p.id=s.platform_id ORDER BY p.key,s.name').fetchall()
    sources = [dict(zip(('id','name','url','platform','origin','enabled'), row)) for row in rows]
    target.write_text(json.dumps({'sources':sources,'provenance':'legacy_database_readonly'},ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'sources':len(sources),'platforms':dict(collections.Counter(s['platform'] for s in sources))}))

if __name__ == '__main__':
    try: main()
    except Exception as error:
        message=str(error).lower()
        category=next((tag for tag,needle in [('authentication_failed','password authentication failed'),
            ('tenant_unavailable','tenant or user not found'),('startup_parameter','unsupported startup parameter'),
            ('database_paused','paused'),('timeout','timeout'),('schema_missing','does not exist'),
            ('circuit_breaker','circuit breaker'),('authentication_failed','authentication'),
            ('ssl_closed','ssl connection has been closed'),('ssl_error','ssl'),
            ('connection_refused','connection refused'),('pool_capacity','maxclients'),
            ('network_name','could not translate'),('server_closed','server closed'),
            ('invalid_configuration','invalid'),('permission_denied','not allowed')] if needle in message),'unclassified')
        print('Catalog export failed:', type(error).__name__, category, 'SQLSTATE:',getattr(error,'sqlstate',None))
        raise SystemExit(1)
