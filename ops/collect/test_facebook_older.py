import unittest
from facebook_older import candidate, media_stories, source_tab, strictly_older


class OlderTests(unittest.TestCase):
    def test_other_post_requires_earlier_publication_and_distinct_native_id(self):
        head={'key':'facebook:abc','date':'2026-11-01T10:00:00+00:00','story_id':'123'}
        row={'canonical_key':'facebook:def','published_at':'2026-10-30T15:30:00+00:00','facebook_story_id':'456'}
        self.assertTrue(strictly_older(row,head))
        for bad in ({'published_at':None},{'published_at':head['date']},{'facebook_story_id':'123'},{'canonical_key':head['key']}):
            self.assertFalse(strictly_older({**row,**bad},head))

    def test_seo_permalink_and_media_parent_scope(self):
        self.assertEqual(candidate('https://www.facebook.com/example/posts/title-here/12345/')['canonical_key'],'facebook:12345')
        self.assertIsNone(candidate('https://x.test/example/posts/title-here/12345/'))
        self.assertIsNone(candidate('https://www.facebook.com/example/posts/title-here/12345/?comment_id=55'))
        media=[{'__typename':'Photo','id':'7','creation_story':{'post_id':'20'}},
               {'__typename':'Photo','id':'9','creation_story':{'post_id':'30'}}]
        self.assertEqual(list(media_stories(media,'7')),[{'post_id':'20'}])
        self.assertEqual(source_tab('https://www.facebook.com/profile.php?id=123','photos'),'https://www.facebook.com/profile.php?id=123&sk=photos')
