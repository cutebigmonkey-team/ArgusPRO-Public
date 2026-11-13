"""Pin the exact frontend code tested by the existing Hostless build service."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip()
if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=source,text=True).strip():
    raise SystemExit('Commit the frontend changes before taking a validation snapshot')
target = Path(__file__).resolve().parents[1]/'validation/comments-ui'
assets = source/'frontend/cloud'
paths = sorted([*assets.glob('*.mjs'), assets/'tv-catalog.json'])
manifest = {'files':{}}
for path in paths:
    relative = path.relative_to(source).as_posix()
    output = target/relative
    output.parent.mkdir(parents=True,exist_ok=True)
    # Hash canonical committed bytes, independent of checkout line endings.
    output.write_bytes(subprocess.check_output(['git','show',revision+':'+relative],cwd=source))
    manifest['files'][relative] = hashlib.sha256(output.read_bytes()).hexdigest()
manifest['source_tree_sha256'] = hashlib.sha256(json.dumps(dict(sorted(manifest['files'].items())),separators=(',',':')).encode()).hexdigest()
(target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
print('Verified frontend content snapshot')
