import asyncio
from collections import Counter
import unittest
from platform_workers import run_workers


class PlatformWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_platform_does_not_block_sibling_and_parallelism_is_bounded(self):
        active=Counter();peak=Counter();finished=[]
        release=asyncio.Event();fast_done=asyncio.Event()
        async def one(source):
            p=source['platform'];active[p]+=1;peak[p]=max(peak[p],active[p])
            if p=='facebook':await release.wait()
            else:
                await asyncio.sleep(0)
                fast_done.set()
            finished.append(source['id']);active[p]-=1
        sources=[{'id':str(i),'platform':p} for p in ('facebook','telegram') for i in range(20)]
        task=asyncio.create_task(run_workers(sources,one,{'telegram':2}))
        try:
            await asyncio.wait_for(fast_done.wait(),1)
            self.assertFalse(task.done())
        finally:release.set()
        await task
        self.assertEqual(peak,{'facebook':1,'telegram':2})
        self.assertEqual(len(finished),40)

    async def test_parser_failure_finishes_other_jobs_before_reporting_failure(self):
        finished=[]
        async def one(source):
            if source['id']==0:raise ValueError('parser')
            finished.append(source['id'])
        with self.assertRaises(ExceptionGroup):
            await run_workers([{'id':i,'platform':'rss'} for i in range(4)],one,{'rss':2})
        self.assertEqual(set(finished),{1,2,3})

    async def test_cancellation_reaches_active_platform_workers(self):
        started=asyncio.Event();cancelled=asyncio.Event()
        async def one(source):
            started.set()
            try:await asyncio.Event().wait()
            finally:cancelled.set()
        task=asyncio.create_task(run_workers([{'platform':'rss'}],one,{}))
        await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertTrue(cancelled.is_set())
