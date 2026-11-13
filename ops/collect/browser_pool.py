"""Start Chromium only on first browser fallback; bound live contexts."""
import asyncio


class LazyRuntime:
    """Public feeds do not start a Playwright Node driver on every microbatch."""
    def __init__(self,factory):
        self.factory=factory;self.manager=None;self.runtime=None;self.lock=asyncio.Lock()
        self.chromium=self
    async def __aenter__(self):return self
    async def launch(self,**kwargs):
        async with self.lock:
            if self.runtime is None:
                self.manager=self.factory()
                self.runtime=await self.manager.__aenter__()
        return await self.runtime.chromium.launch(**kwargs)
    async def __aexit__(self,*args):
        if self.runtime is not None:
            await self.manager.__aexit__(*args)
            self.runtime=None


class LazyBrowser:
    def __init__(self, chromium, *, executable_path=None, contexts=1):
        self.chromium=chromium
        self.executable_path=executable_path
        self.browser=None
        self.launch_lock=asyncio.Lock()
        self.slots=asyncio.Semaphore(contexts)

    async def new_context(self, **kwargs):
        await self.slots.acquire()
        try:
            async with self.launch_lock:
                if self.browser is None:
                    self.browser=await self.chromium.launch(headless=True,executable_path=self.executable_path)
            context=await self.browser.new_context(**kwargs)
            return _Context(context,self.slots)
        except BaseException:
            self.slots.release()
            raise

    async def close(self):
        if self.browser is not None: await self.browser.close()


class _Context:
    def __init__(self,context,slots):
        self.context,self.slots=context,slots
        self.closed=False

    def __getattr__(self,name):
        return getattr(self.context,name)

    async def close(self):
        if self.closed:return
        self.closed=True
        try:await self.context.close()
        finally:self.slots.release()
