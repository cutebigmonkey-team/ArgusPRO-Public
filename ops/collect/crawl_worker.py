"""One bounded Crawlee or Scrapy job per process. Invoked by enhanced.py."""
import asyncio
import json
from pathlib import Path
import sys
from crawl_common import CrawlState


async def crawlee_run(state):
    from crawlee.crawlers import BasicCrawler
    from crawlee import ConcurrencySettings
    from datetime import timedelta
    crawler=BasicCrawler(max_requests_per_crawl=state.max_pages,max_request_retries=0,
        use_session_pool=False,retry_on_blocked=False,configure_logging=False,
        concurrency_settings=ConcurrencySettings(min_concurrency=1,max_concurrency=1,desired_concurrency=1),
        request_handler_timeout=timedelta(seconds=70))

    @crawler.router.default_handler
    async def handle(context):
        if state.stopped:return
        try:body,url=await asyncio.to_thread(state.read,context.request.url)
        except Exception:
            links=state.next_urls()
            if links:await crawler.add_requests(links)
            return  # read() records categorized errors; blocked pages are not retried.
        links=state.consume(body,url)
        if links:await crawler.add_requests(links)

    @crawler.failed_request_handler
    async def failed(context,error):
        state.errors.append('crawler_handler_failed')

    await crawler.run([state.source['url']])


def scrapy_run(state):
    import scrapy
    from scrapy.crawler import CrawlerProcess
    from scrapy.http import HtmlResponse
    from scrapy.exceptions import IgnoreRequest
    from twisted.internet.threads import deferToThread
    from scrapy import signals

    class BoundedDownloader:
        def process_request(self,request,spider=None):
            async_result=deferToThread(state.read,request.url)
            def response(value):
                body,url=value
                return HtmlResponse(url=url,body=body,request=request)
            def failure(error):raise IgnoreRequest('bounded_fetch_failed')
            async_result.addCallbacks(response,failure)
            return async_result

    class Articles(scrapy.Spider):
        name='argus_catalog_articles'
        async def start(self):
            yield scrapy.Request(state.source['url'],callback=self.parse,errback=self.failed)
        def parse(self,response):
            for link in state.consume(response.body,response.url):
                yield scrapy.Request(link,callback=self.parse,errback=self.failed)
        def failed(self,failure):
            for link in state.next_urls():
                yield scrapy.Request(link,callback=self.parse,errback=self.failed)

    process=CrawlerProcess({'LOG_ENABLED':False,'TELNETCONSOLE_ENABLED':False,
        'CONCURRENT_REQUESTS':1,'RETRY_ENABLED':False,'REDIRECT_ENABLED':False,
        'COOKIES_ENABLED':False,'DOWNLOAD_TIMEOUT':12,'CLOSESPIDER_TIMEOUT':75,
        'DOWNLOADER_MIDDLEWARES':{BoundedDownloader:50},
        'TWISTED_REACTOR':'twisted.internet.asyncioreactor.AsyncioSelectorReactor'})
    crawler=process.create_crawler(Articles)
    def failed(failure,response,spider):state.errors.append('crawler_handler_failed')
    crawler.signals.connect(failed,signal=signals.spider_error,weak=False)
    process.crawl(crawler);process.start(install_signal_handlers=False)


def main():
    engine,request,output=sys.argv[1:]
    args=json.loads(Path(request).read_text(encoding='utf-8'))
    state=CrawlState(**args)
    if engine=='crawlee':asyncio.run(crawlee_run(state))
    elif engine=='scrapy':scrapy_run(state)
    else:raise ValueError('unknown_engine')
    Path(output).write_text(json.dumps(state.result(),ensure_ascii=False),encoding='utf-8')


if __name__=='__main__':main()
