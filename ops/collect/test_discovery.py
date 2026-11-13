import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from social_links import identity,extract,from_posts,merge_records
from discovery_probe import search_rows,snapshot,TOOLS

STAMP='2026-10-30T14:00:00+00:00'


class DiscoveryTests(unittest.TestCase):
    def test_only_post_urls_and_exact_hosts(self):
        for url in ('https://www.instagram.com/example/','https://instagram.com.evil/p/ABCDE/',
                    'https://instagram.com@evil.example/p/ABCDE/','https://www.facebook.com/sharer.php?u=x',
                    'https://www.facebook.com/share/p/ABC/','https://www.facebook.com/a/posts/42?comment_id=9'):
            self.assertIsNone(identity(url),url)
        self.assertEqual(identity('https://m.facebook.com/a/posts/42/?utm_source=x')['canonical_key'],'facebook:42')
        self.assertEqual(identity('https://instagram.com/example/p/ABCDE/?igsh=x')['url'],'https://www.instagram.com/p/ABCDE/')
        self.assertEqual(identity('https://www.facebook.com/permalink.php?story_fbid=42&id=7&__cft__=x')['canonical_key'],'facebook:42')

    def test_hidden_anchor_targets_retained_tracking_removed(self):
        text='<a href="https://www.instagram.com/p/ABCDE/?utm_source=tg">Фото</a><script>https://facebook.com/a/posts/9/</script>'
        self.assertEqual(extract(text),['https://www.instagram.com/p/ABCDE/'])
        self.assertEqual(extract('Посилання: https://www.facebook.com/a/posts/42/.'),['https://www.facebook.com/a/posts/42/'])

    def test_telegram_and_feed_preserve_anchor_targets(self):
        from run import telegram_html,feed_posts
        source={'id':'tg','name':'Example','platform':'telegram','url':'https://t.me/example'}
        html='<div class="tgme_widget_message" data-post="example/1"><div class="tgme_widget_message_text"><a href="https://instagram.com/p/ABCDE/">Фото</a></div></div>'
        item=telegram_html(source,5,html)[0]
        self.assertEqual(item['outbound_social_links'],['https://www.instagram.com/p/ABCDE/'])
        feed='<rss version="2.0"><channel><item><link>https://example.com/news/1</link><description><![CDATA[<a href="https://facebook.com/a/posts/42/">Допис</a>]]></description></item></channel></rss>'
        item=feed_posts({**source,'platform':'rss','url':'https://example.com'},feed,5)[0]
        self.assertEqual(item['outbound_social_links'],['https://www.facebook.com/a/posts/42/'])

    def test_provenance_no_borrowed_text_or_date(self):
        post={'id':'telegram:a/1','platform':'telegram','url':'https://t.me/a/1','observed_at':STAMP,
              'text':'Чужий текст','published_at':STAMP,'outbound_social_links':['https://instagram.com/p/ABCDE/']}
        row=from_posts([post])[0]
        self.assertEqual(row['text'],''); self.assertIsNone(row['published_at'])
        self.assertEqual(row['evidence'][0]['source_url'],post['url'])
        self.assertEqual(from_posts([post],{'instagram'}),[])
        self.assertEqual(from_posts([{**post,'sample':True}]),[])

    def test_search_snippet_not_post_body_and_wrong_platform_rejected(self):
        query={'platform':'instagram','q':'site:instagram.com/p/ ДСНС'}
        rows=search_rows(query,{'results':[{'url':'https://instagram.com/p/ABCDE/','content':'Уривок','publishedDate':STAMP},
            {'url':'https://facebook.com/a/posts/42/','content':'Other'}]},10,STAMP)
        self.assertEqual(len(rows),1); self.assertEqual(rows[0]['snippet'],'Уривок')
        self.assertEqual(rows[0]['text'],''); self.assertIsNone(rows[0]['published_at'])

    def test_search_continuation_survives_empty_and_error(self):
        from discovery_probe import query_pages
        q={'platform':'instagram','q':'site:instagram.com/p/ example'}
        cursors={}
        calls=[]
        def reader(query,language,page):
            calls.append(page)
            if page==2 and len(calls)==2:raise TimeoutError()
            return {'results':[{'url':'https://instagram.com/p/ABCDE/'}] if page==2 else []}
        _,first=query_pages(q,cursors,reader)
        self.assertEqual(first['status'],'search_failed')
        rows,second=query_pages(q,cursors,reader)
        self.assertEqual(calls,[1,2,2,3,4])
        self.assertEqual((len(rows),second['status'],cursors),(1,'empty_confirmed',{}))

    def test_one_blocked_engine_does_not_hide_remaining_result_pages(self):
        from discovery_probe import query_pages
        q={'platform':'instagram','q':'site:instagram.com'}
        def read(q,lang,page):
            return {'results':[{'url':f'https://instagram.com/p/ABCDE{page}/'}],
                    'unresponsive_engines':[['google','access denied']] if page==1 else []}
        cursors={}
        rows,report=query_pages(q,cursors,read,budget=3)
        self.assertEqual((len(rows),report['next_page']),(3,4))

    def test_dedup_keeps_evidence_and_replay(self):
        post={'id':'telegram:a/1','platform':'telegram','url':'https://t.me/a/1','observed_at':STAMP,
              'text':'https://instagram.com/p/ABCDE/'}
        row=from_posts([post])[0]
        search=search_rows({'platform':'instagram','q':'ДСНС'},{'results':[{'url':row['url'],'content':'Уривок'}]},10,STAMP)
        merged=merge_records([],[row]+search)
        self.assertEqual(len(merged),1); self.assertEqual(len(merged[0]['evidence']),2)
        self.assertEqual(merge_records(merged,[row]+search),merged)

    def test_login_or_hidden_content_never_baseline(self):
        source={'platform':'facebook','url':'https://www.facebook.com/example'}
        baseline,result=snapshot(source,'<form>Log in</form><div hidden><a href="https://facebook.com/example/posts/42/">Hidden post text</a></div>')
        self.assertIsNone(baseline); self.assertEqual(result['status'],'no_visible_posts')

    @unittest.skipUnless((TOOLS/'changedetection').exists(),'Pinned changedetection modules are installed in hosted CI')
    def test_actual_changedetection_baseline_change_and_replay(self):
        source={'platform':'rss','url':'https://example.com/'}
        body='<main><h1>Публічні новини</h1><p>Рятувальники провели навчання та перевірили обладнання.</p></main>'
        state,result=snapshot(source,body)
        self.assertEqual(result['status'],'baseline_created')
        _,result=snapshot(source,body,state); self.assertEqual(result['status'],'unchanged')
        _,result=snapshot(source,body.replace('навчання','тренування'),state)
        self.assertEqual(result['status'],'changed'); self.assertIn('тренування',result['diff'])
        _,result=snapshot(source,body,{**state,'upstream_commit':'old'})
        self.assertEqual(result['status'],'baseline_reset')


if __name__=='__main__': unittest.main()
