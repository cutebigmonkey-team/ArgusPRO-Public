import asyncio
import unittest
from unittest.mock import AsyncMock,Mock
from browser_pool import LazyBrowser


class PoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_only_does_not_launch_chromium(self):
        chromium=Mock();chromium.launch=AsyncMock()
        pool=LazyBrowser(chromium)
        await pool.close();chromium.launch.assert_not_awaited()

    async def test_one_context_and_cancelled_waiter_does_not_leak_slot(self):
        chromium=Mock();chromium.launch=AsyncMock()
        chromium.launch.return_value.new_context=AsyncMock()
        chromium.launch.return_value.close=AsyncMock()
        pool=LazyBrowser(chromium)
        first=await pool.new_context()
        waiter=asyncio.create_task(pool.new_context())
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):await waiter
        await first.close()
        second=await asyncio.wait_for(pool.new_context(),.5)
        await second.close();await pool.close()
        chromium.launch.assert_awaited_once()
