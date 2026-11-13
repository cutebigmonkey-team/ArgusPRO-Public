"""Install only allowlisted app and shell files; never include collectors or user data."""
import argparse
import hashlib
from pathlib import Path
import shutil
import time
import zipfile

SHELL = {'index.html','app.js','app.wasm','styles.css','archive.js','archive.css','bootstrap.js','connection.js'}
APP = ('storage.py','schema.sql','archive_api.py','auth_store.py','web_api.py','setup-api.py','manage-users.py')
LAUNCHERS = ('Start-Argus.ps1','Stop-Argus.ps1','Open-Argus.cmd','Open-Argus-Test.cmd','Stop-Argus.cmd',
             'Stop-PostgreSQL.cmd','Manage-Users.cmd','Receive-From-GitHub.cmd','Import-Package.cmd')
WHEEL = 'waitress-3.0.2-py3-none-any.whl'
WHEEL_SHA = 'c56d67fd6e87c2ee598b76abdd4e96cfad1f24cacdea5078d382b1f9d7b5ed2e'

def install(root, shell):
    root, shell = Path(root).resolve(), Path(shell).resolve()
    source = Path(__file__).resolve().parent
    wheel = source/'vendor'/WHEEL
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != WHEEL_SHA:
        raise ValueError('Waitress checksum mismatch')
    expected = shell.with_suffix('.zip.sha256').read_text().strip()
    if hashlib.sha256(shell.read_bytes()).hexdigest() != expected:
        raise ValueError('Shell checksum mismatch')
    with zipfile.ZipFile(shell) as bundle:
        if set(bundle.namelist()) != SHELL or len(bundle.namelist()) != len(SHELL):
            raise ValueError('Unexpected shell files')
        if sum(i.file_size for i in bundle.infolist()) > 20*1024**2:
            raise ValueError('Unexpected shell size')
        payload = {name:bundle.read(name) for name in SHELL}
    if payload['app.wasm'][:4] != b'\0asm': raise ValueError('Invalid WASM')
    # Protected backup of exactly the files replaced, not a live database copy.
    backup = root/'private'/('app-backup-'+str(time.time_ns()))
    replacements = [Path('app')/name for name in APP] + [Path('web')/name for name in SHELL]
    replacements += [Path(name) for name in LAUNCHERS] + [Path('private/archive-api.json'),Path('private/archive-api-test.json')]
    for relative in replacements:
        old = root/relative
        if old.is_file():
            dest = backup/relative
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(old,dest)
    for folder in ('app/vendor','web','logs'):
        (root/folder).mkdir(parents=True,exist_ok=True)
    for name in APP: shutil.copy2(source/name,root/'app'/name)
    for name in LAUNCHERS: shutil.copy2(source/name,root/name)
    shutil.copy2(wheel,root/'app/vendor'/WHEEL)
    for name,data in payload.items(): (root/'web'/name).write_bytes(data)
    print('Local app installed; databases and media preserved. Collectors are not installed.')

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',required=True)
    parser.add_argument('--shell',required=True)
    args=parser.parse_args()
    install(args.root,args.shell)
