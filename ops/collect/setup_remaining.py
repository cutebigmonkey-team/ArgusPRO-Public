"""Install pinned extra sources; all builds and collection stay on hosted runners."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from cloud_social import require_cloud

require_cloud()
root = Path('.social-tools').resolve()
root.mkdir(exist_ok=True)
for name, entry in json.loads(Path(__file__).with_name('remaining-upstreams.json').read_text()).items():
    if os.environ.get('FACEBOOK_ONLY') == 'true' and name != 'fbgql':
        continue
    target = root / name
    subprocess.run(['git', 'init', str(target)], check=True)
    subprocess.run(['git', '-C', str(target), 'fetch', '--depth=1',
                    'https://github.com/' + entry['repository'] + '.git', entry['commit']], check=True)
    subprocess.run(['git', '-C', str(target), 'checkout', '--detach', 'FETCH_HEAD'], check=True)
    actual = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != entry['commit']:
        raise RuntimeError('Upstream revision mismatch')
if os.environ.get('FACEBOOK_ONLY') == 'true':
    raise SystemExit(0)
shutil.copyfile(Path(__file__).with_name('ArgusOffline.kt'),
    root/'skraper/cli/src/main/kotlin/ru/sokomishalov/skraper/cli/ArgusOffline.kt')
# Argus decodes the HTTP charset and writes UTF-8 for all offline parsers.
# The pinned VK parser otherwise hard-codes Windows-1251 and corrupts Ukrainian.
vk = root/'skraper/skrapers/src/main/kotlin/ru/sokomishalov/skraper/provider/vk/VkSkraper.kt'
original = vk.read_text()
if original.count('charset = WIN_1251_ENCODING') != 1:
    raise RuntimeError('Unexpected pinned VK parser')
vk.write_text(original.replace('charset = WIN_1251_ENCODING', 'charset = Charsets.UTF_8'))
with open(os.environ['GITHUB_ENV'], 'a') as env:
    env.write('ARGUS_SOCIAL_TOOLS=' + str(root) + '\n')
