"""Install reviewed upstream revisions on a hosted runner only."""
import json
from pathlib import Path
import subprocess
from cloud_social import require_cloud

require_cloud()
root=Path('.discovery-tools').resolve(); root.mkdir(exist_ok=True)
for name,entry in json.loads(Path(__file__).with_name('discovery-upstreams.json').read_text()).items():
    target=root/name
    subprocess.run(['git','init',str(target)],check=True)
    subprocess.run(['git','-C',str(target),'fetch','--depth=1','https://github.com/'+entry['repository']+'.git',entry['commit']],check=True)
    subprocess.run(['git','-C',str(target),'checkout','--detach','FETCH_HEAD'],check=True)
    if subprocess.check_output(['git','-C',str(target),'rev-parse','HEAD'],text=True).strip()!=entry['commit']:
        raise ValueError('upstream_revision_mismatch')
