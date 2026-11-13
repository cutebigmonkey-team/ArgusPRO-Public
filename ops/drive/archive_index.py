"""Disposable, bounded-cache read model. Drive remains the archive of record.

The index is rebuilt from checksum-verified Drive objects on every process start.
No acknowledged cursor or pending payload relies on this file surviving restart.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from drive_store import encoded
from url_identity import identity_keys


class Rows(list):
    # JSONEncoder.iterencode recognizes list subclasses and iterates lazily.
    def __init__(self,index): self.index=index
    def __len__(self): return self.index.count()
    def __iter__(self): return self.index.rows()
    def __bool__(self): return bool(len(self))


class ArchiveIndex:
    def __init__(self,path,*,max_bytes=384*1024**2):
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.execute('PRAGMA cache_size=-2048')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('PRAGMA mmap_size=0')
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA max_page_count='+str(max_bytes//4096))
        self.db.execute('CREATE TABLE posts(id TEXT PRIMARY KEY,discovered TEXT,published TEXT,payload BLOB NOT NULL)')
        self.db.execute('CREATE TABLE aliases(key TEXT PRIMARY KEY,id TEXT NOT NULL)')
        self.db.execute('CREATE INDEX recent_publications ON posts(published)')
        self.db.execute('CREATE INDEX discovery_order ON posts(discovered DESC,id DESC)')

    def put(self,item,*,initial=False):
        sid=item['id']
        sql='INSERT INTO posts VALUES(?,?,?,?)' if initial else 'INSERT INTO posts VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET discovered=excluded.discovered,published=excluded.published,payload=excluded.payload'
        self.db.execute(sql,(sid,item.get('discovered_at') or item.get('observed_at') or '',item.get('published_at'),encoded(item)))
        # Exact legacy IDs take precedence over aliases, independent of load order.
        self.db.execute('INSERT INTO aliases VALUES(?,?) ON CONFLICT(key) DO UPDATE SET id=excluded.id',(sid,sid))
        for key in identity_keys(item):
            self.db.execute('INSERT INTO aliases VALUES(?,?) ON CONFLICT(key) DO NOTHING',(key,sid))

    def after(self,cursor=None,limit=128):
        for row in self.db.execute('SELECT id,payload FROM posts WHERE id>? ORDER BY id LIMIT ?',
                                   (cursor or '',limit)):
            yield row[0],json.loads(row[1])

    def __contains__(self,key): return self.resolve(key) is not None
    def resolve(self,key):
        row=self.db.execute('SELECT id FROM aliases WHERE key=?',(key,)).fetchone()
        return row[0] if row else None
    def get(self,key):
        sid=self.resolve(key)
        row=self.db.execute('SELECT payload FROM posts WHERE id=?',(sid,)).fetchone() if sid else None
        return json.loads(row[0]) if row else None
    def count(self): return self.db.execute('SELECT COUNT(*) FROM posts').fetchone()[0]
    def rows(self):
        for row in self.db.execute('SELECT payload FROM posts ORDER BY discovered DESC,id DESC'): yield json.loads(row[0])
    def news(self):
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
        for row in self.db.execute('SELECT payload FROM posts WHERE published>=?',(cutoff,)): yield json.loads(row[0])
    def touched(self,posts):
        found={}
        for post in posts:
            for key in identity_keys(post):
                old=self.get(key)
                if old is not None:found[old['id']]=old
        return list(found.values())
    def close(self): self.db.close()
