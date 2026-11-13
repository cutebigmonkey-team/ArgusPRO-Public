import unittest
from verify_collection import unique_items, verify_posts, agency_report


class ArchiveVerificationTests(unittest.TestCase):
    def setUp(self):
        self.post = {'canonical_key':'youtube:abcdefghijk', 'platform':'youtube', 'post_url':'https://www.youtube.com/watch?v=abcdefghijk',
                     'text':'Real publication', 'published_at':None, 'observed_at':'2026-10-30T10:00:00Z'}
        self.item = {'id':self.post['canonical_key'], 'platform':'youtube', 'url':self.post['post_url'],
                     'text':self.post['text'], 'published_at':None, 'observed_at':self.post['observed_at'], 'sample':False}

    def test_missing_or_changed_archive_content_fails(self):
        expected = {self.post['canonical_key']:self.post}
        for items in ([], [{**self.item, 'text':'Wrong text'}], [{**self.item, 'sample':True}]):
            with self.assertRaises(ValueError): verify_posts(expected, {'items':items})
        result = verify_posts(expected, {'items':[self.item]})
        self.assertEqual(result['exact_matches'], 1)

    def test_tiktok_readback_checks_complete_caption_and_retained_analysis(self):
        from record_quality import video_caption
        key='tiktok:42'
        post={**self.post,'canonical_key':key,'platform':'tiktok','post_url':'https://www.tiktok.com/@example/video/42'}
        item={**self.item,'id':key,'platform':'tiktok','url':post['post_url'],'video_analysis_text':'Transcript',
              'text':post['text']+'\n\nTranscript'}
        self.assertEqual(verify_posts({key:post},{'items':[item]})['retained_richer_records'],1)
        with self.assertRaisesRegex(ValueError,'archive_content_mismatch'):
            verify_posts({key:post},{'items':[{**item,'text':'Different caption\n\nTranscript'}]})
        self.assertEqual(video_caption('x'*100000,'Transcript'),'x'*100000)

    def test_short_and_watch_duplicates_fail_even_under_different_keys(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_canonical_keys'):
            unique_items([self.item, self.item])
        alias = {**self.item, 'id':'youtube:legacy-hash', 'url':'https://www.youtube.com/shorts/abcdefghijk'}
        with self.assertRaisesRegex(ValueError, 'duplicate_platform_identities'):
            unique_items([self.item, alias])

    def test_newer_versions_are_distinguished_from_exact_matches(self):
        item = {**self.item, 'observed_at':'2026-10-30T11:00:00Z', 'text':'Updated'}
        result = verify_posts({self.post['canonical_key']:self.post}, {'items':[item]})
        self.assertEqual(result['newer_versions'], 1); self.assertEqual(result['exact_matches'], 0)

    def test_channel_alias_dedup_keeps_agency_coverage(self):
        channel = 'UC'+'a'*22
        sources = [{'id':'named', 'platform':'youtube', 'url':'https://www.youtube.com/@example'},
                   {'id':'direct', 'platform':'youtube', 'url':'https://www.youtube.com/channel/'+channel}]
        config = {'youtube_channels':{'named':{'channel_id':channel}},
                  'agencies':[{'agency':'Agency', 'website_source_id':'site', 'alternatives':['named']}]}
        result = agency_report(config, {'sources':sources}, [{'source_id':'named','status':'ok','posts':1}],
                               {'direct':{'youtube:abcdefghijk'}}, {'youtube:abcdefghijk'})
        self.assertEqual(result[0]['archived_posts'], 1)

    def test_telegram_post_url_alias_retains_channel_provenance(self):
        sources = [{'id':'channel', 'platform':'telegram', 'url':'https://t.me/dsns_example'},
                   {'id':'post', 'platform':'telegram', 'url':'https://t.me/DSNS_Example/123'}]
        config = {'youtube_channels':{},
                  'agencies':[{'agency':'Agency', 'website_source_id':'site', 'alternatives':['channel']}]}
        result = agency_report(config, {'sources':sources}, [{'source_id':'channel','status':'ok','posts':1}],
                               {'post':{'telegram:dsns_example/456'}}, {'telegram:dsns_example/456'})
        self.assertEqual(result[0]['archived_posts'], 1)

    def test_focused_catalog_does_not_claim_uncollected_alternatives(self):
        sources=[{'id':'channel','platform':'telegram','url':'https://t.me/example'}]
        config={'youtube_channels':{},'agencies':[{'agency':'Agency','website_source_id':'site','alternatives':['channel','youtube-absent']}]}
        result=agency_report(config,{'sources':sources},[{'source_id':'channel','status':'ok','posts':1}],
                             {'channel':{'telegram:example/1'}},{'telegram:example/1'})
        self.assertEqual([a['source_id'] for a in result[0]['alternatives']],['channel'])
        self.assertEqual(result[0]['archived_posts'],1)


if __name__ == '__main__': unittest.main()
