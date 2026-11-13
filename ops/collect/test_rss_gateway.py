import unittest
import asyncio
from unittest.mock import patch, AsyncMock
import urllib.error
import rss_gateway as gateway

SOURCE={'id':'ig-test','platform':'instagram','url':'https://www.instagram.com/example','name':'Example'}


def feed(entries,profile='example'):
    return f'<feed xmlns="http://www.w3.org/2005/Atom"><title>Profile</title><link href="https://www.instagram.com/{profile}/"/>{entries}</feed>'


def entry(code='Ab_C',author='example',url=None):
    return f'''<entry><title>Short title</title><link href="{url or 'https://www.instagram.com/p/'+code+'/'}"/>
    <author><name>{author}</name></author><published>2026-10-21T05:04:15Z</published>
    <content type="html">Full caption &lt;img src="https://cdn.example.org/image.jpg"/&gt;</content></entry>'''


class GatewayTests(unittest.TestCase):
    def test_identity_content_date_and_dedup(self):
        rows=gateway.parse(feed(entry()+entry(url='https://www.instagram.com/reel/Ab_C/')),SOURCE,5)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['canonical_key'],'instagram:Ab_C')
        self.assertEqual(rows[0]['text'],'Full caption')
        self.assertEqual(rows[0]['published_at'],'2026-10-21T05:04:15+00:00')
        self.assertEqual(rows[0]['media_urls'],['https://cdn.example.org/image.jpg'])
        self.assertEqual(rows[0]['gateway_url'],gateway.GATEWAY)

    def test_reject_wrong_profile_author_and_error_items(self):
        self.assertEqual(gateway.parse(feed(entry(),'someone_else'),SOURCE,5),[])
        self.assertEqual(gateway.parse(feed(entry(author='someone_else')),SOURCE,5),[])
        self.assertEqual(gateway.parse(feed(entry(url=gateway.GATEWAY)),SOURCE,5),[])
        self.assertEqual(gateway.parse(feed(entry(url='https://www.instagram.com:444/p/Ab_C/')),SOURCE,5),[])

    def test_missing_date_is_not_observation_time(self):
        rows=gateway.parse(feed(entry().replace('<published>2026-10-21T05:04:15Z</published>','')),SOURCE,5)
        self.assertIsNone(rows[0]['published_at'])

    def test_rate_limit_stops_gateway_for_remaining_run(self):
        error=urllib.error.HTTPError(gateway.GATEWAY,429,'Too many',{},None)
        with patch.object(gateway,'_limited',False),patch.object(gateway,'_last_finished',0),patch('run.fetch',side_effect=error) as fetch:
            with self.assertRaises(urllib.error.HTTPError):gateway.collect(SOURCE,5)
            with self.assertRaisesRegex(ValueError,'gateway_rate_limited'):gateway.collect(SOURCE,5)
            self.assertEqual(fetch.call_count,1)

    def test_invalid_profile_not_requested(self):
        with patch('run.fetch') as fetch:
            with self.assertRaises(ValueError):gateway.collect({**SOURCE,'url':'https://www.instagram.com/accounts/login/'},5)
            fetch.assert_not_called()

    def test_collector_prefers_gateway_and_falls_back_on_empty_feed(self):
        import run
        browser=AsyncMock(return_value=[])
        with patch.object(gateway,'collect',return_value=[{'canonical_key':'instagram:Ab_C'}]),patch('run.social',browser):
            items,status,attempts=asyncio.run(run.collect_source(None,SOURCE,5))
        browser.assert_not_called()
        self.assertEqual((status,attempts[0]['tool']),('ok','rss_bridge'))
        with patch.object(gateway,'collect',return_value=[]),patch('run.social',browser):
            items,status,attempts=asyncio.run(run.collect_source(None,SOURCE,5))
        browser.assert_awaited_once()
        self.assertEqual(status,'no_public_posts')


if __name__=='__main__':unittest.main()
