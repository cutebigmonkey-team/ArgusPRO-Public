"""Pin reviewed parsing libraries on an ephemeral hosted runner."""
import json
import os
from pathlib import Path
import subprocess
from cloud_social import require_cloud

require_cloud()
root = Path('.social-tools').resolve()
root.mkdir(exist_ok=True)
for name, entry in json.loads(Path(__file__).with_name('embed-upstreams.json').read_text()).items():
    if entry.get('install') is False:
        continue
    target = root/name
    subprocess.run(['git','init',str(target)],check=True)
    subprocess.run(['git','-C',str(target),'fetch','--depth=1','https://github.com/'+entry['repository']+'.git',entry['commit']],check=True)
    subprocess.run(['git','-C',str(target),'checkout','--detach','FETCH_HEAD'],check=True)
    if subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip() != entry['commit']:
        raise RuntimeError('Upstream revision mismatch')
with open(os.environ['GITHUB_ENV'],'a') as env:
    env.write('ARGUS_SOCIAL_TOOLS='+str(root)+'\n')
