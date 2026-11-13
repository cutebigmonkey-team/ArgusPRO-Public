"""Paced starts with bounded platform concurrency for cloud HTTP readers."""
import asyncio
import time
from contextlib import asynccontextmanager


class Admission:
    def __init__(self,pacing,parallelism=None):
        self.pacing=pacing;self.parallelism=parallelism or {}
        self.slots={key:asyncio.Semaphore(self.parallelism.get(key,1)) for key in pacing}
        self.locks={key:asyncio.Lock() for key in pacing}
        self.last={}

    @asynccontextmanager
    async def enter(self,key):
        if key not in self.slots:
            yield
            return
        async with self.slots[key]:
            async with self.locks[key]:
                delay=self.pacing[key]-(time.monotonic()-self.last.get(key,0))
                if delay>0:await asyncio.sleep(delay)
                if self.parallelism.get(key,1)>1:self.last[key]=time.monotonic()
            try:yield
            finally:
                if self.parallelism.get(key,1)==1:self.last[key]=time.monotonic()
