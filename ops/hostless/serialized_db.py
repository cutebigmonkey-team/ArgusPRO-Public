"""Serialize auxiliary SQL with the coordinator's existing session/transactions."""
from contextlib import contextmanager


class Connection:
    def __init__(self,db,lock):self.db=db;self.lock=lock
    def execute(self,*args,**kwargs):
        with self.lock:return self.db.execute(*args,**kwargs)
    @contextmanager
    def transaction(self):
        with self.lock:
            with self.db.transaction():yield
    def cursor(self):return Cursor(self.db.cursor(),self.lock)


class Cursor:
    def __init__(self,cursor,lock):self.cursor=cursor;self.lock=lock
    def executemany(self,*args,**kwargs):
        with self.lock:return self.cursor.executemany(*args,**kwargs)
    def close(self):
        with self.lock:self.cursor.close()
