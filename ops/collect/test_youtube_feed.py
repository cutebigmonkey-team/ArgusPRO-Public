import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from youtube_feed import page_channel, parse, resolve
import run

OWNER = 'UC' + 'a'*22
OTHER = 'UC' + 'b'*22
VIDEO = 'c'*11


def feed(owner=OWNER, entry_owner=OWNER, video=VIDEO):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
    <yt:channelId>{owner[2:]}</yt:channelId><link rel="alternate" href="https://www.youtube.com/channel/{owner}"/>
    <entry><yt:channelId>{entry_owner}</yt:channelId><yt:videoId>{video}</yt:videoId><title>Example</title>
    <link href="https://www.youtube.com/watch?v={video}"/><published>2026-10-29T12:00:00Z</published></entry></feed>'''.encode()


class YouTubeFeedTests(unittest.TestCase):
    def setUp(self):
        self.source = {'id':'fixture', 'platform':'youtube', 'url':'https://www.youtube.com/@example'}
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.fixture_root=Path(temporary.name)
        sources=[{'id':'fixture-channel','platform':'youtube','url':self.source['url'],'enabled':True}]
        agencies=[]
        for i in range(12):
            website={'id':'fixture-web'+str(i),'platform':'rss','url':'https://example.invalid/site/'+str(i)}
            sources.append(website)
            agencies.append({'agency':'Fixture '+str(i),'website_source_id':website['id'],'website_url':website['url'],
                             'alternatives':['fixture-channel'] if i==0 else []})
        config={'agencies':agencies,'youtube_channels':{'fixture-channel':{'source_url':self.source['url'],'channel_id':OWNER}}}
        (self.fixture_root/'sources.json').write_text(json.dumps({'sources':sources}))
        (self.fixture_root/'dsns-official-sources.json').write_text(json.dumps(config))
        for module in ('youtube_feed','dsns_coverage'):
            patcher=patch(module+'.private_asset_path',side_effect=lambda name:self.fixture_root/name)
            patcher.start();self.addCleanup(patcher.stop)


    def test_recommended_video_channel_is_never_owner(self):
        body = ('<script>{"channelId":"'+OTHER+'","metadata":{"channelMetadataRenderer":{"externalId":"'+OWNER+'"}}}</script>').encode()
        self.assertEqual(page_channel(body), OWNER)
        with self.assertRaisesRegex(ValueError, 'channel_owner_unresolved'):
            page_channel(('"channelId":"'+OTHER+'"').encode())

    def test_conflicting_owner_metadata_rejected(self):
        body = (f'<meta itemprop="channelId" content="{OTHER}"><script>{{"channelMetadataRenderer":{{"externalId":"{OWNER}"}}}}</script>').encode()
        with self.assertRaisesRegex(ValueError, 'channel_owner_unresolved'): page_channel(body)

    def test_feed_and_entry_owners_are_checked(self):
        item = parse(self.source, feed(), OWNER, 5)[0]
        self.assertEqual(item['canonical_key'], 'youtube:'+VIDEO)
        self.assertEqual(item['source_id'], 'fixture')
        self.assertEqual(item['platform'], 'youtube')
        for body in (feed(OTHER), feed(entry_owner=OTHER), feed(video='invalid')):
            with self.assertRaises(ValueError): parse(self.source, body, OWNER, 5)

    def test_existing_collector_uses_native_atom_with_ownership(self):
        source = {**self.source, 'url':'https://www.youtube.com/channel/'+OWNER}
        with patch.object(run, 'fetch', return_value=(feed(), source['url'])) as fetch:
            self.assertEqual(run.feeds(source, 5)[0]['capture_mode'], 'youtube_native_atom')
            fetch.assert_called_once_with('https://www.youtube.com/feeds/videos.xml?channel_id='+OWNER)

    def test_one_foreign_entry_does_not_discard_verified_siblings(self):
        foreign=feed(entry_owner=OTHER,video='x'*11).split(b'<entry>')[1].split(b'</entry>')[0]
        body=feed().replace(b'</feed>',b'<entry>'+foreign+b'</entry></feed>')
        self.assertEqual([p['canonical_key'] for p in parse(self.source,body,OWNER,5)],['youtube:'+VIDEO])

    def test_native_shorts_and_watch_have_same_identity(self):
        shorts = feed().replace(('watch?v='+VIDEO).encode(), ('shorts/'+VIDEO).encode())
        self.assertEqual(parse(self.source, shorts, OWNER, 5)[0]['canonical_key'], 'youtube:'+VIDEO)
        mismatch = shorts.replace(('shorts/'+VIDEO).encode(), ('shorts/'+'d'*11).encode())
        with self.assertRaises(ValueError): parse(self.source, mismatch, OWNER, 5)

    def test_mapping_contains_only_fixture_sources_and_verified_owner(self):
        root = self.fixture_root
        catalog = {s['id']:s for s in json.loads((root/'sources.json').read_text(encoding='utf-8'))['sources']}
        config = json.loads((root/'dsns-official-sources.json').read_text(encoding='utf-8'))
        self.assertEqual(len(config['agencies']), 12)
        for identity, pin in config['youtube_channels'].items():
            self.assertEqual(catalog[identity]['url'], pin['source_url'])
            with patch('run.fetch', side_effect=AssertionError('Pinned channel should not fetch its profile')):
                self.assertEqual(resolve(catalog[identity], run.fetch), pin['channel_id'])
        for agency in config['agencies']:
            self.assertEqual(catalog[agency['website_source_id']]['url'], agency['website_url'])
            for identity in agency['alternatives']:
                self.assertTrue(catalog[identity].get('enabled', True))
            if agency['website_url'] == 'https://example.invalid/site/0':
                self.assertEqual(agency['alternatives'], ['fixture-channel'])

    def test_coverage_does_not_claim_website_recovery_or_freshness_for_old_posts(self):
        from dsns_coverage import coverage
        source = 'fixture-channel'
        website = 'fixture-web0'
        result = coverage([{'source_id':website, 'status':'robots_challenge_required'},
                           {'source_id':source, 'status':'ok'}],
                          [{'source_id':source, 'published_at':'2025-01-01T00:00:00+00:00'}],
                          '2026-10-30T00:00:00+00:00')
        agency = next(a for a in result['agencies'] if a['website_source_id'] == website)
        self.assertTrue(agency['covered']); self.assertFalse(agency['recent_covered'])
        self.assertEqual(agency['website_status'], 'robots_challenge_required')

    def test_coverage_survives_cross_source_post_deduplication(self):
        from dsns_coverage import coverage
        source = 'fixture-channel'
        result = coverage([{'source_id':source, 'status':'ok', 'posts':5,
                            'latest_published_at':'2026-10-29T00:00:00+00:00'}], [], '2026-10-30T00:00:00+00:00')
        self.assertEqual(result['covered'], 1)
        self.assertEqual(result['recent_covered'], 1)


if __name__ == '__main__': unittest.main()
