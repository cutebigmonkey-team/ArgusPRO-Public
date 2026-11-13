"""Run genuine offline Gitleaks scopes, or explicitly report a blocked gate.

Private logs/reports and scope copies live under .git, never Docker/public artifacts.
No credentials are verified online. No repository/CI/production mutation is made.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import io


def run(root,binary=None):
    output=root/'.git/publication-validation/independent';output.mkdir(parents=True,exist_ok=True,mode=0o700)
    binary=binary or shutil.which('gitleaks')
    scopes=['all_tracked_files','all_git_history','docker_context_superset','workflow_files','committed_fixtures']
    if not binary:
        result={'tool':'Gitleaks','executed':False,'status':'BLOCKED_BY_ENVIRONMENT',
                'reason':'established_scanner_not_available','scopes':{s:'NOT_EXECUTED' for s in scopes}}
        (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
        return result
    binary=str(Path(binary).resolve())
    tracked=output/'tracked';context=output/'docker-context-superset';committed=output/'committed-fixtures'
    # Never reuse earlier scope copies, which could falsely include deleted secrets.
    for directory in (tracked,context,committed):
        if directory.exists():raise ValueError('independent_scan_output_already_exists')
        directory.mkdir()
    for raw in subprocess.check_output(['git','ls-files','-z'],cwd=root).split(b'\0'):
        if not raw:continue
        path=Path(os.fsdecode(raw));source=root/path
        if source.is_symlink():raise ValueError('symlink_requires_independent_review')
        if source.is_file():
            target=tracked/path;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    # Conservative superset: all source files except Git metadata. Actual Docker
    # ignore rules can only reduce this set. Never claim an engine-generated context.
    for source in root.rglob('*'):
        relative=source.relative_to(root)
        if '.git' in relative.parts:continue
        if source.is_symlink():raise ValueError('symlink_requires_independent_review')
        if source.is_file():
            target=context/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    archive=subprocess.check_output(['git','archive','HEAD','--','ops'],cwd=root)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        for entry in stream:
            if not entry.isfile():continue
            target=(committed/entry.name).resolve()
            if not target.is_relative_to(committed.resolve()):raise ValueError('unsafe_git_archive_path')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(stream.extractfile(entry).read())
    commands={
        'all_tracked_files':['dir',str(tracked)],
        'all_git_history':['git','--log-opts=--all',str(root)],
        'docker_context_superset':['dir',str(context)],
        'workflow_files':['dir',str(root/'.github/workflows')],
        # Includes all committed ops, a superset of embedded/config fixture files.
        'committed_fixtures':['dir',str(committed)],
    }
    outcomes={}
    for scope,args in commands.items():
        path=output/(scope+'.json')
        command=[binary,*args,'--redact=100','--no-banner','--no-color','--ignore-gitleaks-allow',
                 '--max-archive-depth=3','--max-decode-depth=3','--timeout=120',
                 '--report-format=json','--report-path='+str(path)]
        log=output/(scope+'.log')
        fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as sink:
            try:code=subprocess.run(command,cwd=root,stdout=sink,stderr=subprocess.STDOUT,timeout=130).returncode
            except subprocess.TimeoutExpired:code=124
        if path.exists():path.chmod(0o600)
        outcomes[scope]={'exit_code':code,'status':'PASSED' if code==0 else 'FAILED_OR_INCOMPLETE'}
    result={'tool':'Gitleaks','executed':True,'scopes':outcomes,
            'status':'PASSED' if all(v['exit_code']==0 for v in outcomes.values()) else 'NOT_PASSED',
            'docker_scope':'conservative source superset, not an engine-exported context'}
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path('.'));args=parser.parse_args()
    result=run(args.root.resolve())
    print(json.dumps(result))
    raise SystemExit(0 if result['status']=='PASSED' else 1)

if __name__=='__main__':main()
