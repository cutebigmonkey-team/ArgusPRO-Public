"""Bounded, retryable comment initialization/sweep outside collector admission.

Jobs and comment versions stay in existing SQL. Missed sync hints are recovered
by a keyset sweep of the verified archive index, including after process restart.
"""
import time
from collections import OrderedDict
from startup import Startup,error_type,known_code


class CommentMaintenance:
    def __init__(self,db,feed,emit,guard,clock=time.monotonic):
        self.db,self.feed,self.emit,self.guard,self.clock=db,feed,emit,guard,clock
        self.comments=None;self.cursor=None;self.generation=0;self.sweeping_generation=-1
        self.complete=False;self.failures=0;self.retry_at=0;self.status='pending';self.hints=OrderedDict()
    def dirty(self,ids=()):
        changed=False
        for sid in ids:
            changed=True
            if len(self.hints)>=1024:break  # overflow is recovered by the archive sweep
            self.hints[sid]=None
        if changed:self.rescan()
    def rescan(self):self.generation+=1;self.complete=False
    def step(self,sources):
        if self.clock()<self.retry_at:return
        try:
            self.guard()
            if self.comments is None:
                with Startup(self.emit).phase('comments_initialization'),self.db.transaction():
                    from comments_store import Comments
                    comments=Comments(self.db)
                self.comments=comments
            hints=list(self.hints)[:128]
            if hints:
                with Startup(self.emit).phase('comments_synchronization'):
                    self.comments.sync([row for sid in hints if (row:=self.feed.index.get(sid))],sources)
                for sid in hints:self.hints.pop(sid,None)
            if not self.complete:
                if self.cursor is None:self.sweeping_generation=self.generation
                with Startup(self.emit).phase('comments_synchronization'):
                    rows=list(self.feed.index.after(self.cursor,128))
                    if rows:
                        self.comments.sync([row for _,row in rows],sources)
                        self.cursor=rows[-1][0]
                    if len(rows)<128:
                        self.cursor=None;self.complete=self.sweeping_generation==self.generation
                self.emit('comments_sync_progress',rows=len(rows),sweep_complete=self.complete)
            self.failures=0;self.retry_at=0;self.status='ok' if self.complete else 'partial'
        except Exception as error:
            self.guard()  # A lost database session must stop the entire writer.
            self.failures+=1;self.retry_at=self.clock()+min(300,2**min(self.failures,8))
            self.status='degraded'
            self.emit('comments_maintenance_failed',error_code=known_code(error) or 'comments_maintenance_failed',
                      error_type=error_type(error),retry_seconds=min(300,2**min(self.failures,8)))
    def close(self):
        if self.comments is not None:self.comments.close()
