"""Bounded temporary replay of already verified pages, fetched once each.

Scratch is never a persistent checkpoint. Every remote page is verified by the
existing reader before being spooled, then checked for local corruption on replay.
"""
import hashlib
import json
import tempfile
from drive_store import encoded


class VerifiedReplay:
    def __init__(self,max_pages=512,max_bytes=256*1024**2):
        self.file=tempfile.TemporaryFile();self.entries=[];self.size=0
        self.max_pages,self.max_bytes=max_pages,max_bytes
    def append(self,page):
        raw=encoded(page)
        if len(self.entries)>=self.max_pages or self.size+len(raw)>self.max_bytes:
            raise ValueError('verified_replay_capacity_exceeded')
        self.entries.append((self.size,len(raw),hashlib.sha256(raw).digest()))
        self.file.write(raw);self.size+=len(raw)
    def __iter__(self):
        self.file.flush()
        for offset,size,digest in reversed(self.entries):
            self.file.seek(offset);raw=self.file.read(size)
            if len(raw)!=size or hashlib.sha256(raw).digest()!=digest:
                raise ValueError('verified_replay_scratch_corruption')
            yield json.loads(raw)
    def __len__(self):return len(self.entries)
    def __enter__(self):return self
    def __exit__(self,*args):self.file.close()
