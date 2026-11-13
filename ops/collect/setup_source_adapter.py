"""Check out a single pinned executable dependency on a disposable runner."""
import json
import os
from pathlib import Path
import subprocess
import sys
import secrets
from urllib.parse import urlunsplit

from setup_social import checkout



def fixture_services():
    password=secrets.token_urlsafe(24)
    # JSON is a supported YAML subset. The upstream service expects both stores.
    services = {
        'db': {'image': 'postgres:16-alpine', 'environment': {'POSTGRES_DB': 'pravaha',
               'POSTGRES_USER': 'pravaha', 'POSTGRES_PASSWORD': password},
               'healthcheck': {'test': ['CMD', 'pg_isready', '-U', 'pravaha'], 'interval': '2s', 'retries': 20}},
        'redis': {'image': 'redis:7-alpine', 'healthcheck': {'test': ['CMD', 'redis-cli', 'ping'],
                  'interval': '2s', 'retries': 20}},
        'api': {'build': {'context':'./api', 'dockerfile':'Argus.Dockerfile'}, 'ports': ['127.0.0.1:4000:4000'], 'environment': {'PORT': '4000',
                'HOST':'0.0.0.0', 'DATABASE_URL': urlunsplit(('postgres', 'pravaha:'+password+'@db:5432', '/pravaha', '', '')),
                'REDIS_URL':'redis://redis:6379'}, 'depends_on':
                {name:{'condition':'service_healthy'} for name in ('db','redis')}}
    }
    return services


def write_fixture_compose(path):
    path=Path(path)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump({'services':fixture_services()},stream)

if __name__ == '__main__':
    if os.getenv('GITHUB_ACTIONS') != 'true' or os.getenv('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise SystemExit('Requires a GitHub-hosted runner')
    tool, = sys.argv[1:]
    if tool not in ('pravaha', 'threads-cli', 'scrapedog-cobalt'):
        raise SystemExit('Unknown tool')
    root = Path('.social-tools')
    root.mkdir(exist_ok=True)
    pins = json.loads(Path(__file__).with_name('source-adapters.json').read_text())
    checkout(root, {tool: pins[tool]})
    if tool == 'scrapedog-cobalt':
        # version-info reads the repository URL from .git/config at startup.
        subprocess.run(['git', '-C', str(root/tool), 'remote', 'add', 'origin',
                        'https://github.com/' + pins[tool]['repository'] + '.git'], check=True)
    if tool == 'pravaha':
        (root/tool/'api/Argus.Dockerfile').write_text(
            'FROM node:22-alpine\nWORKDIR /app\nCOPY package*.json ./\nRUN npm ci\n'
            'COPY . .\nCMD ["sh","-c","./node_modules/.bin/tsx src/db/migrate.ts && exec ./node_modules/.bin/tsx src/server.ts"]\n'
        )
        write_fixture_compose(root/tool/'argus-compose.yml')
