import asyncio
import time
import unittest
from source_admission import Admission


class AdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_http_readers_overlap_without_unpaced_starts(self):
        admission=Admission({'telegram':.015},{'telegram':2})
        active=peak=0;starts=[]
        async def job():
            nonlocal active,peak
            async with admission.enter('telegram'):
                starts.append(time.monotonic());active+=1;peak=max(peak,active)
                await asyncio.sleep(.05);active-=1
        await asyncio.gather(*(job() for _ in range(6)))
        self.assertEqual(peak,2)
        self.assertTrue(all(b-a>=.014 for a,b in zip(starts,starts[1:])))

    async def test_serial_platform_keeps_pause_after_completion(self):
        admission=Admission({'instagram':.015})
        active=peak=0;starts=[];ends=[]
        async def job():
            nonlocal active,peak
            async with admission.enter('instagram'):
                starts.append(time.monotonic());active+=1;peak=max(peak,active)
                await asyncio.sleep(.01);active-=1;ends.append(time.monotonic())
        await asyncio.gather(*(job() for _ in range(3)))
        self.assertEqual(peak,1)
        self.assertTrue(all(start-end>=.014 for start,end in zip(starts[1:],ends)))

    async def test_cancelled_job_releases_its_platform_slot(self):
        admission=Admission({'youtube':0},{'youtube':2})
        started=asyncio.Event()
        async def job():
            async with admission.enter('youtube'):
                started.set();await asyncio.sleep(30)
        task=asyncio.create_task(job());await started.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        async with admission.enter('youtube'):
            async with admission.enter('youtube'):pass
