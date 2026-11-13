import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock
from browser_pool import LazyRuntime


class LazyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_feed_only_path_never_starts_driver(self):
        factory=Mock()
        async with LazyRuntime(factory):pass
        factory.assert_not_called()

    async def test_browser_launch_and_error_stop_driver_once(self):
        manager=SimpleNamespace(__aenter__=AsyncMock(),__aexit__=AsyncMock())
        chromium=SimpleNamespace(launch=AsyncMock(return_value='browser'))
        manager.__aenter__.return_value=SimpleNamespace(chromium=chromium)
        factory=Mock(return_value=manager)
        with self.assertRaises(RuntimeError):
            async with LazyRuntime(factory) as runtime:
                self.assertEqual(await runtime.chromium.launch(headless=True),'browser')
                raise RuntimeError('fixture')
        factory.assert_called_once();manager.__aexit__.assert_awaited_once()
