import asyncio
import json
import unittest
from unittest.mock import patch,AsyncMock
from urllib.parse import parse_qs,urlsplit
from catalog_scope import select,REQUESTED
from telegram_history import collect,next_before
from run import telegram_html
from max_sdk import normalize

SOURCE={'id':'tg-example','name':'Example','url':'https://t.me/example','platform':'telegram'}


def page(ids,before=None):
    html=''.join('<div class="tgme_widget_message" data-post="example/'+str(n)+'"><div class="tgme_widget_message_text">Post '+str(n)+'</div></div>' for n in ids)
    if before:html+='<a class="tme_messages_more" href="/s/example?before='+str(before)+'">Earlier</a>'
    return html.encode()


class FullCollectionTests(unittest.TestCase):
    def test_search_rotation_covers_every_source_and_resumes(self):
        from discovery_plan import plan,advance
        sources=[{'id':str(i),'platform':p,'url':'https://www.'+p+'.com/user'+str(i)} for p in ('instagram','facebook') for i in range(20)]
        first,cursor=plan(sources,budget=16)
        second,cursor=plan(sources,advance(cursor,len(first)),budget=16)
        third,cursor=plan(sources,advance(cursor,len(second)),budget=16)
        self.assertEqual(len(first+second+third),40)
        self.assertEqual(len({(q['platform'],q['source_id']) for q in first+second+third}),40)
        self.assertEqual(advance(cursor,len(third))['offset'],0)
        blocked,_=plan(sources,disabled={'instagram'},budget=16)
        self.assertTrue(all(q['platform']=='facebook' for q in blocked))

    def run_history(self,pages,previous=None):
        calls=[]
        def fetch(url):
            before=parse_qs(urlsplit(url).query).get('before',[None])[0]
            before=int(before) if before else None; calls.append(before)
            value=pages[before]
            if isinstance(value,Exception):raise value
            return value,url
        with patch('cloud_social.require_cloud'):
            result=collect(SOURCE,previous,fetcher=fetch,parser=lambda source,body,limit:telegram_html(source,limit,body),pause=lambda _:None)
        return result,calls

    def test_all_supplied_sources_are_selected_not_first_few(self):
        sources=[{'id':str(i),'platform':p} for p in REQUESTED for i in range(20)]
        source={'sources':sources+[{'platform':'youtube','id':'other'}]}
        self.assertEqual(select(source,True)['sources'],sources)
        self.assertEqual(select(source,False)['sources'],source['sources'])

    def test_unparsed_owned_page_does_not_commit_a_cursor_or_report_quiet_source(self):
        with patch('cloud_social.require_cloud'):
            empty_card=b'<div class="tgme_widget_message" data-post="example/42"></div>'
            result=collect(SOURCE,fetcher=lambda url:(empty_card,url),parser=lambda *args:[],pause=lambda _:None)
        self.assertEqual(result[1],'unverified_markup')
        self.assertIsNone(result[3])

    def test_partial_success_preserves_missing_native_cards_before_cursor_advance(self):
        body=page([41,42])
        with patch('cloud_social.require_cloud'):
            rows,status,_,cursor=collect(SOURCE,fetcher=lambda url:(body,url),
                parser=lambda source,body,limit:telegram_html(source,1,body))
        self.assertEqual({r['canonical_key'] for r in rows},{'telegram:example/41','telegram:example/42'})
        self.assertEqual(status,'ok')
        self.assertTrue(cursor['exhausted'])

    def test_failed_telegram_parser_keeps_native_cards_and_retries_same_page(self):
        def fail(*args):
            raise ValueError('invalid_output')
        with patch('cloud_social.require_cloud'):
            rows,status,attempts,cursor=collect(SOURCE,fetcher=lambda url:(page([41,42],41),url),parser=fail)
        self.assertEqual([r['canonical_key'] for r in rows],['telegram:example/41','telegram:example/42'])
        self.assertEqual(status,'invalid_output')
        self.assertIsNone(cursor)

    def test_more_than_five_posts_and_backward_progress(self):
        result,calls=self.run_history({None:page(range(21,41),21),21:page(range(11,21),11),11:page(range(1,11))})
        posts,status,attempts,state=result
        self.assertEqual(len(posts),40);self.assertEqual(calls,[None,21,11]);self.assertEqual(status,'ok')
        self.assertTrue(state['exhausted']);self.assertIsNone(state['before'])
        self.assertFalse(attempts[0]['full_platform_history_guaranteed'])

    def test_old_pinned_card_does_not_hide_the_missed_interval(self):
        previous={'head_id':40,'before':None,'exhausted':True}
        result,calls=self.run_history({None:page([1,*range(61,81)],61),
            61:page(range(41,61),41),41:page(range(31,41),31)},previous)
        self.assertEqual(calls,[None,61,41])
        self.assertEqual(result[3]['gaps'],[])
        self.assertTrue({'telegram:example/'+str(i) for i in range(41,81)} <=
                        {row['canonical_key'] for row in result[0]})

    def test_resumed_history_does_not_restart_at_head(self):
        previous={'before':11,'exhausted':False}
        result,calls=self.run_history({None:page(range(41,61),41),11:page(range(1,11))},previous)
        self.assertEqual(calls,[None,11]); self.assertEqual(len(result[0]),30);self.assertTrue(result[3]['exhausted'])

    def test_failed_later_page_keeps_only_last_successful_cursor(self):
        result,calls=self.run_history({None:page(range(21,41),21),21:TimeoutError()})
        self.assertEqual(result[1],'timeout');self.assertEqual(len(result[0]),20)
        self.assertEqual(result[3]['before'],21);self.assertFalse(result[3]['exhausted'])

    def test_empty_or_foreign_page_never_marks_history_complete(self):
        self.assertEqual(next_before(SOURCE,b'<div>No posts</div>'),(None,'no_verified_page'))
        self.assertEqual(next_before(SOURCE,page([10]).replace(b'example/',b'foreign/')),(None,'no_verified_page'))
        result,calls=self.run_history({None:b'<div>No posts</div>'},{'before':11,'exhausted':False})
        self.assertEqual(calls,[None]);self.assertIsNone(result[3])

    def test_exhausted_history_still_refreshes_new_posts(self):
        result,calls=self.run_history({None:page(range(41,61),41)},{'before':None,'exhausted':True})
        self.assertEqual(calls,[None]);self.assertEqual(len(result[0]),20);self.assertEqual(result[3]['head_id'],60)

    def test_missed_heads_resume_gap_separately_from_archived_history(self):
        previous={'before':11,'exhausted':False,'head_id':40}
        result,calls=self.run_history({None:page(range(81,101),81),81:page(range(61,81),61),11:page(range(1,11))},previous)
        self.assertEqual(calls,[None,81,11])
        self.assertEqual(result[3]['gaps'],[[61,40]])
        self.assertTrue(result[3]['exhausted'])
        resumed,calls=self.run_history({None:page(range(81,101),81),61:page(range(41,61),41),41:page(range(21,41),21)},result[3])
        self.assertEqual(calls,[None,61,41])
        self.assertEqual(resumed[3]['gaps'],[])

    def test_expanded_limit_reaches_actual_adapter(self):
        import embed_social,run
        source={'id':'fb','platform':'facebook','url':'https://www.facebook.com/example'}
        with patch.dict('os.environ',{'ARGUS_EMBED_COLLECTION':'1'}),patch.object(embed_social,'collect',new_callable=AsyncMock) as call:
            call.return_value=([{'canonical_key':'facebook:42'}],[{'status':'ok'}])
            asyncio.run(run.collect_source(None,source,100))
            call.assert_awaited_once_with(source,100)

    def test_max_messages_must_belong_to_mapped_channel(self):
        source={'id':'max','name':'Example','platform':'max','url':'https://max.ru/example'}
        row={'recipient':{'chat_id':-7,'chat_type':'channel'},'body':{'mid':'abc','text':'Post'},
             'url':'https://max.ru/c/-7/abc','timestamp':1793361600000}
        self.assertEqual(normalize(source,[row],-7)[0]['canonical_key'],'max:-7/abc')
        self.assertEqual(normalize(source,[row],-8),[])
        row['recipient']['chat_type']='dialog';self.assertEqual(normalize(source,[row],-7),[])


if __name__=='__main__':unittest.main()
