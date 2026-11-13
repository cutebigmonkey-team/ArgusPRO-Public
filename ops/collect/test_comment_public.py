import unittest
from unittest.mock import patch
from comment_public import telegram_html,telegram_public,threads_payloads,structured_comments,x_payload,vk_html


class PublicCommentsTests(unittest.TestCase):
    def test_telegram_discussion_keeps_replies_and_pagination(self):
        target='https://t.me/example/123'
        html='''<h3 class="tgme_post_discussion_header"><a href="https://t.me/example/123">Post</a></h3>
        <div class="tgme_widget_message" data-post-id="9"><span class="tgme_widget_message_author_name">Reader</span>
        <div class="js-message_text">Hello<br>World</div><a class="tgme_widget_message_date"><time datetime="2026-11-05T12:00:00Z"></time></a>
        <a class="tgme_widget_message_reply" href="https://t.me/example/123?comment=8">Quoted text</a></div>
        <div class="tgme_widget_message" data-post="example/123"><div class="js-message_text">Root, not a comment</div></div>
        <div class="js-messages_more" data-before="8"></div>'''
        result=telegram_html(html,target)
        self.assertEqual(result['rows'][0]['text'],'Hello\nWorld');self.assertEqual(len(result['rows']),1)
        self.assertEqual(result['rows'][0]['parent_id'],'8');self.assertEqual(result['cursor'],{'before':'8'})
        self.assertEqual(telegram_html(html,'https://t.me/elsewhere/456')['reason'],'discussion_post_mismatch')
    def test_telegram_empty_is_not_automatically_success(self):
        self.assertEqual(telegram_html('<html/>','https://t.me/example/123')['status'],'failed')
        self.assertEqual(telegram_html('<script>TWidgetDiscussion.init({"comments_cnt":0});</script>','https://t.me/example/123')['status'],'success')
        self.assertEqual(telegram_html('<div class="tme_no_messages_found">Please open Telegram to view this discussion</div>','https://t.me/example/123')['status'],'forbidden')
    def test_telegram_cursor_never_sends_to_external_api(self):
        body='''<div class="tgme_widget_message" data-post-id="9"><div class="js-message_text">Comment</div></div>
        <script>TWidgetAuth.init({"api_url":"https://evil.invalid/api"});</script>'''
        with patch('comment_public.fetch_page',return_value=(body,'https://t.me/example/123?embed=1',None)),patch('http_pool.POOL.request') as request:
            result=telegram_public({'url':'https://t.me/example/123'},{'before':'8'})
        request.assert_not_called();self.assertEqual(len(result['rows']),1)
    def test_threads_scopes_new_ssr_reply_connections_to_target_root(self):
        def reply(identity,text):return dict(pk=identity,code='Code'+identity,user={'username':'reader'},caption={'text':text},taken_at=1793880000)
        child=reply('80','Nested');parent=reply('70','Direct')
        parent['text_post_app_info']={'direct_replies':{'edges':[{'node':child}]}}
        data=[{'media':{'id':'66_123','text_post_app_info':{'direct_replies':{'edges':[{'node':parent}],'page_info':{'has_next_page':True}}}}},
            {'media':{'pk':'999','text_post_app_info':{'direct_replies':{'edges':[{'node':reply('90','Unrelated')} ]}}}}]
        result=threads_payloads({'url':'https://www.threads.com/@author/post/ABC/media'},data)
        self.assertEqual([r['id'] for r in result['rows']],['70','80'])
        self.assertIsNone(result['rows'][0]['parent_id']);self.assertEqual(result['rows'][1]['parent_id'],'70')
        self.assertEqual(result['status'],'partial')
    def test_threads_empty_requires_explicit_zero_and_verified_connection(self):
        root={'pk':'66','code':'ABC','text_post_app_info':{'direct_replies':{'edges':[]}}}
        post={'url':'https://www.threads.com/@author/post/ABC'}
        self.assertNotEqual(threads_payloads(post,[root])['status'],'success')
        root['text_post_app_info']['direct_reply_count']=0
        self.assertEqual(threads_payloads(post,[root])['status'],'success')
        root['text_post_app_info']['has_unavailable_replies']=True
        self.assertNotEqual(threads_payloads(post,[root])['status'],'success')
    def test_structured_comments_reject_other_publications_and_keep_nested(self):
        comment={'@type':'Comment','@id':'c1','text':'Top','comment':[{'@type':'Comment','@id':'c2','text':'Reply'}]}
        rows=structured_comments({'url':'https://example.org/news'},[{'url':'https://example.org/news','comment':[comment]},
            {'url':'https://example.org/other','comment':[{'@type':'Comment','@id':'bad','text':'Other'}]}])
        self.assertEqual([r['id'] for r in rows],['c1','c2']);self.assertEqual(rows[1]['parent_id'],'c1')
    def test_x_only_accepts_explicit_conversation_relationship(self):
        tweet={'rest_id':'2','legacy':{'conversation_id_str':'1','in_reply_to_status_id_str':'1','full_text':'Reply'},
            'core':{'user_results':{'result':{'legacy':{'screen_name':'reader'}}}}}
        rows=x_payload({'url':'https://x.com/author/status/1'},[tweet])
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['parent_id'])
        tweet['legacy']['conversation_id_str']='999';self.assertEqual(x_payload({'url':'https://x.com/author/status/1'},[tweet]),[])
    def test_vk_requires_exact_wall_container(self):
        html='<div id="post-123_5"><div class="reply" id="post-123_6"><div class="wall_reply_text">Hello</div></div></div>'
        self.assertEqual(len(vk_html({'url':'https://vk.com/wall-123_5'},html)),1)
        self.assertEqual(vk_html({'url':'https://vk.com/wall-123_9'},html),[])

if __name__=='__main__':unittest.main()
