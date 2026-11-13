import unittest
from rss_probe import candidates,destination

class RSSProbeTests(unittest.TestCase):
    def test_gateway_error_feed_is_not_a_social_post(self):
        body='<rss version="2.0"><channel><item><title>InstagramBridge failed</title><link>https://rss-bridge.org/error</link></item></channel></rss>'
        result=candidates(body,{'platform':'instagram','url':'https://www.instagram.com/example'})
        self.assertEqual(result['feed_entries'],1);self.assertEqual(result['candidate_posts'],0)

    def test_original_post_identity_and_x_owner_are_required(self):
        body='<rss version="2.0"><channel><item><link>https://x.com/example/status/123</link></item><item><link>https://x.com/other/status/456</link></item><item><link>https://x.com/example/status/123</link></item></channel></rss>'
        result=candidates(body,{'platform':'x','url':'https://x.com/example'})
        self.assertEqual(result['candidate_posts'],1);self.assertIsNone(result['latest_published_at'])

    def test_local_exception_is_only_for_the_probe_container(self):
        destination('http://127.0.0.1:1200/instagram/user/example',True)
        for url in ('http://127.0.0.1:8000/','http://169.254.169.254/','http://user:secret@127.0.0.1:1200/'):
            with self.assertRaises(ValueError):destination(url,True)

if __name__=='__main__':unittest.main()
