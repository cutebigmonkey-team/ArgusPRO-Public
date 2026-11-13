import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from import_sheet import normalize,extract
from web_pages import article_rows
from merge import merge
from storage import pack

class SheetTests(unittest.TestCase):
    def test_case_sensitive_public_links_and_scoped_login_target(self):
        for url in ('https://www.facebook.com/fixture406','https://www.instagram.com/p/AbCd',
                    'https://www.threads.com/@user/post/AbCd'):
            self.assertEqual(normalize(url)[1],url)
        self.assertEqual(normalize('https://www.instagram.com/accounts/login/?next=%2Ffixture606'),
                         ('instagram','https://www.instagram.com/fixture606'))
        with self.assertRaises(ValueError):
            normalize('https://www.instagram.com/accounts/login/?next=https%3A%2F%2Fforeign.example')
    def test_tracking_dedup_retains_profile_id_and_channel_case(self):
        self.assertEqual(normalize('https://www.facebook.com/profile.php?id=123&locale=uk_UA'),('facebook','https://www.facebook.com/profile.php?id=123'))
        self.assertEqual(normalize('https://t.me/s/DSNS?before=123'),('telegram','https://t.me/dsns'))
        self.assertEqual(normalize('https://web.telegram.org/k/#@DSNS'),('telegram','https://t.me/dsns'))
        self.assertEqual(normalize('https://fixture.invalid/source/51?__cf_chl_tk=expired'),('rss','https://fixture.invalid/source/51'))
        self.assertIn('UCAbC',normalize('https://youtube.com/channel/UCAbC')[1])
        with self.assertRaisesRegex(ValueError,'public_username'):normalize('https://web.telegram.org/a/#-100123456')

    def test_homepage_and_undated_jsonld_are_not_posts(self):
        self.assertEqual(article_rows('<h1>Website</h1>','https://example.com'),[])
        self.assertEqual(article_rows('<script type="application/ld+json">{"@type":"Article","headline":"Title"}</script>','https://example.com'),[])
        html='<script type="application/ld+json">{"@type":"NewsArticle","headline":"Title","datePublished":"2026-10-29T12:00:00Z","url":"https://example.com/news/1","articleBody":"Real article"}</script>'
        self.assertEqual(article_rows(html,'https://example.com/news/1')[0]['text'],'Title\n\nReal article')

    def test_missing_shard_sources_cannot_publish_and_shared_posts_deduplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);catalog=root/'sources.json'
            catalog.write_text(json.dumps({'provenance':'test','sources':[{'id':'a','platform':'telegram'},{'id':'b','platform':'telegram'}]}))
            digest=hashlib.sha256(catalog.read_bytes()).hexdigest()
            for i,identity in enumerate(('a','b')):
                folder=root/f'argus-platform-part-{i}';folder.mkdir();(folder/'media').mkdir()
                item={'canonical_key':'telegram:channel/1','platform':'telegram','post_url':'https://t.me/channel/1','text':'Shared','published_at':None,'observed_at':'2026-10-29T12:00:00Z','media':[]}
                (folder/'posts.jsonl').write_text(json.dumps(item)+'\n');pack(folder/'posts.jsonl',folder/'media',folder/'package.zip')
                report={'shard_index':i,'shard_count':2,'catalog_sha256':digest,'run_id':'1','wall_seconds':1,
                        'sources':[{'source_id':identity,'platform':'telegram','status':'ok','posts':1}]}
                (folder/'report.json').write_text(json.dumps(report))
            merge(root,catalog,root/'out',2)
            report=json.loads((root/'out/report.json').read_text());self.assertEqual(report['unique_posts'],1);self.assertEqual(report['attempted_sources'],2)
            registry=root/'registry.json';registry.write_text('{}')
            with self.assertRaisesRegex(ValueError,'Mismatched collector registry'):merge(root,catalog,root/'wrong-registry',2,registry)
            p=root/'argus-platform-part-1/report.json';r=json.loads(p.read_text())
            r['sources'][0]['canonical_keys']=['telegram:channel/2']
            p.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'Shard package differs'):merge(root,catalog,root/'wrong-package',2)
            p=root/'argus-platform-part-1/report.json';r=json.loads(p.read_text());r['sources']=[];p.write_text(json.dumps(r))
            with self.assertRaisesRegex(ValueError,'Missing enabled sources'):merge(root,catalog,root/'bad',2)

if __name__=='__main__':unittest.main()
