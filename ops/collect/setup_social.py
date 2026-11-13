"""Fetch exact reviewed upstream revisions on a disposable cloud runner."""
import json
import os
from pathlib import Path
import subprocess


def checkout(root, manifest):
    for name, entry in manifest.items():
        target = root / name
        subprocess.run(['git', 'init', str(target)], check=True)
        subprocess.run(['git', '-C', str(target), 'fetch', '--depth=1',
                        'https://github.com/' + entry['repository'] + '.git', entry['commit']], check=True)
        subprocess.run(['git', '-C', str(target), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
        actual = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != entry['commit']:
            raise RuntimeError('Upstream revision mismatch')


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('RUNNER_ENVIRONMENT') != 'github-hosted':
        raise RuntimeError('Social tool installation requires a GitHub-hosted runner')
    root = Path('.social-tools').resolve()
    root.mkdir(exist_ok=True)
    manifest = json.loads(Path(__file__).with_name('social-upstreams.json').read_text())
    checkout(root, manifest)
    with open(os.environ['GITHUB_ENV'], 'a') as env:
        env.write('ARGUS_SOCIAL_TOOLS=' + str(root) + '\n')
        env.write('PYTHONPATH=' + str(root / 'tg-radar' / 'src') + '\n')


if __name__ == '__main__':
    main()
