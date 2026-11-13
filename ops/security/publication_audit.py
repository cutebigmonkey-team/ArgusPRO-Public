"""Redacted local publication audit. Never outputs matching content or values.

Heuristics are not proof of absence. Remote-only refs, encrypted objects and
provider artifacts require separate authorized review. Exit 1 blocks publication.
"""
import argparse
from collections import Counter
import gzip
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import zipfile

RULES={
 'private_key_material':rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
 'github_token_candidate':rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
 'provider_key_candidate':rb'\b(?:hlk_[A-Za-z0-9_-]{24,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|AIza[A-Za-z0-9_-]{30,})\b',
 'database_credential_candidate':rb'(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?)://[^\s\x22\x27<>:@]+:[^\s\x22\x27<>@]+@[^\s\x22\x27<>]+',
 'google_credential_document':rb'\x22type\x22\s*:\s*\x22service_account\x22',
 'sensitive_google_identifier':rb'https?://(?:drive|docs)\.google\.com/(?:[^\s\x22\x27<>]*/)?(?:d|folders)/[A-Za-z0-9_-]{16,}',
 'sensitive_registry_identifier':rb'https?://api\.jsonbin\.io/v\d+/b/[A-Za-z0-9]{16,}',
}
ASSIGN=re.compile(rb'(?i)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|secret[_-]?key)\s*[\x22\x27]?\s*[:=]\s*[\x22\x27]([^\x22\x27\r\n]{16,})[\x22\x27]')

def example(value):
 low=value.lower()
 return any(v in low for v in (b'example',b'fixture',b'placeholder',b'changeme',b'dummy',b'localhost',b'127.0.0.1',b'${',b'your_',b'os.environ',b'getenv(',b'process.env'))

def inspect(data):
 findings=[]
 for rule,pattern in RULES.items():
  for m in re.finditer(pattern,data):
   if not example(m.group()):findings.append({'category':rule,'line':data.count(b'\n',0,m.start())+1})
 for m in ASSIGN.finditer(data):
  value=m[1]
  if example(value):continue
  frequencies=Counter(value);entropy=-sum((n/len(value))*math.log2(n/len(value)) for n in frequencies.values())
  if entropy>=3.5:findings.append({'category':'assigned_secret_candidate','line':data.count(b'\n',0,m.start())+1})
 return findings


def inspect_payload(data):
 findings=inspect(data);gaps=[]
 if data.startswith(b'PK\x03\x04'):
  try:
   with zipfile.ZipFile(io.BytesIO(data)) as archive:
    total=0
    for member in archive.infolist():
     if member.is_dir():continue
     total+=member.file_size
     if member.flag_bits&1 or member.file_size>4*1024**2 or total>16*1024**2:
      gaps.append('archive_member_uninspected');continue
     for f in inspect(archive.read(member)):
      findings.append({**f,'category':'archive_'+f['category']})
  except (OSError,ValueError,zipfile.BadZipFile,RuntimeError):gaps.append('archive_unreadable')
 elif data.startswith(b'\x1f\x8b'):
  try:
   with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:expanded=stream.read(16*1024**2+1)
   if len(expanded)>16*1024**2:gaps.append('compressed_payload_limit')
   else:findings.extend({'category':'compressed_'+f['category'],'line':f['line']} for f in inspect(expanded))
  except (OSError,EOFError):gaps.append('compressed_payload_unreadable')
 return findings,gaps


def audit(root):
 def git(*args):return subprocess.check_output(['git',*args],cwd=root)
 refs=git('for-each-ref','--format=%(refname)').splitlines()
 names={}
 for line in git('rev-list','--objects','--all').splitlines():
  oid,_,path=line.partition(b' ');names[oid.decode()]=path.decode('utf-8',errors='replace')
 # Include locally stored unreachable objects; they are not normally pushed,
 # but must not be mistaken for a complete sanitized backup.
 for oid in git('cat-file','--batch-all-objects','--batch-check=%(objectname)').splitlines():
  names.setdefault(oid.decode(),'')
 objects=[];counts=Counter();gaps=[]
 proc=subprocess.Popen(['git','cat-file','--batch'],cwd=root,stdin=subprocess.PIPE,stdout=subprocess.PIPE)
 try:
  for oid,path in names.items():
   proc.stdin.write((oid+'\n').encode());proc.stdin.flush()
   header=proc.stdout.readline().split();kind=header[1].decode();size=int(header[2]);data=proc.stdout.read(size);proc.stdout.read(1)
   counts[kind]+=1
   if kind not in {'blob','commit','tag'}:continue
   found,missing=inspect_payload(data)
   synthetic_empty=False
   if kind=='blob' and path.endswith('.json'):
    try:
     value=json.loads(data)
     def empty(v):return all(empty(x) for x in v.values()) if isinstance(v,dict) else all(empty(x) for x in v) if isinstance(v,list) else v in (None,'',False,0)
     synthetic_empty=empty(value)
    except (ValueError,TypeError):pass
   if kind=='blob' and not synthetic_empty and (re.search(r'(^|/)(?:sources|source-reviews|.*snapshot.*|.*diagnostic.*|.*baseline.*)\.(?:json|jsonl|csv|sqlite|db)$',path,re.I) or path.endswith(('.zip','.sqlite','.db'))):
    found.append({'category':'confidential_dataset_or_artifact_review','line':None})
   if found:objects.append({'object':oid,'kind':kind,'path':path if kind=='blob' else None,'findings':found})
   gaps.extend({'object':oid,'reason':g} for g in missing)
 finally:proc.stdin.close();proc.wait()
 current=[]
 files=set(git('ls-files','-z').decode().split('\0'))|set(git('ls-files','--others','--exclude-standard','-z').decode().split('\0'))
 for name in sorted(files- {''}):
  p=root/name
  if not p.is_file():continue
  if p.stat().st_size>16*1024**2:gaps.append({'path':name,'reason':'working_file_size_limit'});continue
  findings,missing=inspect_payload(p.read_bytes())
  if findings:current.append({'path':name,'findings':findings})
  gaps.extend({'path':name,'reason':g} for g in missing)
 categories=Counter(f['category'] for o in objects for f in o['findings'])
 return {'decision':'PUBLICATION_BLOCKED' if objects or current or gaps else 'LOCAL_SCAN_CLEAR_REMOTE_REVIEW_REQUIRED','local_refs_scanned':len(refs),'objects_scanned':dict(counts),
         'history_findings':objects,'working_tree_findings':current,'historical_category_counts':dict(categories),'scan_gaps':gaps,
         'limitations':['All locally stored objects inspected; remote-only branches/tags and provider artifacts not verified.',
                        'Heuristic findings require private review; no match is not proof of no secrets.',
                        'Encrypted/nested/non-ZIP archive formats and binary datasets need manual review.',
                        'Credential liveness and production binding never tested.'],
         'no_values_exported':True}


def main():
 p=argparse.ArgumentParser();p.add_argument('--root',type=Path,default=Path('.'));p.add_argument('--output',type=Path,required=True);args=p.parse_args()
 result=audit(args.root.resolve());args.output.parent.mkdir(parents=True,exist_ok=True)
 fd=os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
 with os.fdopen(fd,'w') as stream:json.dump(result,stream,indent=2)
 print(json.dumps({k:result[k] for k in ('decision','local_refs_scanned','objects_scanned','historical_category_counts','no_values_exported')}))
 raise SystemExit(1)

if __name__=='__main__':main()
