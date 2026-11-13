"""Publish only the validated package and fixed collector result files between jobs."""
from pathlib import Path
import shutil
root=Path('build-tool-smoke');out=Path('cloud-export');out.mkdir(exist_ok=True)
package=root/'export/package.zip'
if package.is_file(): shutil.copyfile(package,out/'package.zip')
for tool in ('yt-dlp','archivebox','sherlock'):
    result=root/'reports'/tool/'result.json'
    if result.is_file():
        destination=out/'reports'/tool/'result.json'
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(result,destination)
