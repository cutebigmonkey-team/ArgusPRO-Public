"""Bound task creation by platform; source admission and SQL retain durability.

Queues contain only the supervisor's bounded admitted batch, not the registry.
Browser and global HTTP gates, pacing and per-source deadlines remain in one().
Unexpected platform defects finish sibling work before reaching the supervisor.
"""
import asyncio
from collections import defaultdict,deque


async def run_workers(sources,one,parallelism):
    queues=defaultdict(deque)
    for source in sources:queues[source['platform']].append(source)
    errors=[]
    async def worker(queue):
        while queue:
            source=queue.popleft()
            try:await one(source)
            except Exception as exc:errors.append(exc)
    tasks=[]
    for platform,queue in queues.items():
        slots=parallelism.get(platform,1)
        if type(slots) is not int or not 1<=slots<=16:raise ValueError('invalid_platform_parallelism')
        tasks.extend(worker(queue) for _ in range(min(slots,len(queue))))
    # Cancellation propagates to all workers. Never consume CancelledError.
    await asyncio.gather(*tasks)
    if errors:raise ExceptionGroup('platform_worker_failures',errors)
