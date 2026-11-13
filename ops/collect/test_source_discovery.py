import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

from catalog_scope import due_batch, recent_live_plan
from source_discovery import (add_registered, apply_reviews, candidate, enroll, inspect_channel, merge_candidates,
                              note_cooldown, profile, publisher_links, scan)
from social_links import outbound
from telegram_history import collect
from run import telegram_html

STAMP='2026-11-03T10:00:00+00:00'
SITE='https://press.test/'
FEED=SITE+'rss.xml'
SOURCE={'id':'press','name':'Press','platform':'rss','url':SITE,'enabled':True,'origin':'general'}
HOME=b'<head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head><footer><a href="https://t.me/newspress">Channel</a></footer>'
XML=b'<rss version="2.0"><channel><item><title>News</title><link>https://press.test/items/1</link></item></channel></rss>'
CHANNEL=b'<div class="tgme_channel_info_description"><a href="https://press.test/">Publisher</a></div><div class="tgme_widget_message" data-post="newspress/8"><div class="tgme_widget_message_poll_question">Question?</div></div>'


class PublicReader:
    pages=0
    stopped=False
    def __init__(self,source,limit,max_pages):
        self.max_pages=max_pages
    def read(self,url):
        self.pages+=1
        return {SITE:HOME, FEED:XML,'https://t.me/s/newspress':CHANNEL}[url],url


class SourceDiscoveryTests(unittest.TestCase):
    def test_public_profiles_and_profile_anchors(self):
        for url in ('https://t.me/+380500000000','https://t.me/joinchat/secret','https://t.me/share',
                    'https://t.me/somebot?start=personal','https://t.me.evil.test/news',
                    'https://instagram.com/accounts/login/?next=/example','https://x.com/name/status/123'):
            self.assertIsNone(profile(url),url)
        self.assertEqual(outbound('<a href="https://twitter.com/Press">editor</a>')['outbound_profile_links'],
                         ['https://x.com/press'])

    def test_discover_enroll_schedule_replay_and_disable(self):
        with patch('source_discovery.DIRECTORIES',{}):
            rows,cursor,attempts=scan([SOURCE],reader=PublicReader)
        self.assertEqual((cursor,attempts[0]['status']),(SITE,'ok'))
        self.assertEqual({r['status'] for r in rows},{'confirmed'})
        catalog=enroll({'sources':[SOURCE]},rows)
        self.assertEqual(len(catalog['sources']),3)
        self.assertEqual(enroll(catalog,rows),catalog)
        merged=add_registered({'sources':[SOURCE]},catalog)
        self.assertEqual(merged,catalog)
        selected=due_batch(merged,{}, {'rss'},datetime.fromisoformat(STAMP))
        self.assertEqual({s['url'] for s in selected['sources']},{SITE,FEED})
        disabled={**catalog,'sources':[dict(s,enabled=False) if s['url']==FEED else s for s in catalog['sources']]}
        self.assertEqual(enroll(disabled,rows),disabled)

    def test_weak_evidence_stays_in_moderation(self):
        html=b'<head><link rel="alternate" type="application/rss+xml" href="https://another.test/feed"></head><a href="https://instagram.com/person">Mention</a>'
        rows=list(publisher_links(html,SITE,STAMP))
        self.assertEqual({r['status'] for r in rows},{'review'})
        self.assertEqual(enroll({'sources':[]},rows)['sources'],[])
        moderator={rows[1]['url']:{'name':'Public newsroom','status':'approved','category':'regional_media',
                   'evidence_url':SITE,'sample_permalink':'https://www.instagram.com/p/POST123/'}}
        approved=apply_reviews(rows,moderator)
        registered=enroll({'sources':[]},approved)
        self.assertEqual(registered['sources'][0]['origin'],'general')
        rejected=apply_reviews(rows,{rows[1]['url']:{'status':'rejected'}})
        self.assertFalse(enroll(registered,rejected)['sources'][0]['enabled'])
        self.assertEqual(merge_candidates(rejected,rows)[1]['status'],'rejected')
        moderator[rows[1]['url']]['category']='dsns'
        with self.assertRaisesRegex(ValueError,'invalid_source_review'):
            apply_reviews(rows,moderator)

    def test_channel_requires_reciprocal_identity(self):
        row=candidate('telegram','https://t.me/newspress','publisher_channel_link','https://other.test/',STAMP)
        inspect_channel(row,PublicReader)
        self.assertEqual((row['status'],row['reason']),('review','publisher_backlink_missing'))

    def test_owned_cards_compare_before_checkpoint(self):
        src={'id':'t','platform':'telegram','url':'https://t.me/newspress'}
        body=CHANNEL+b'<div class="tgme_widget_message" data-post="newspress/9"></div>'
        with patch('cloud_social.require_cloud'):
            posts,status,attempts,checkpoint=collect(src,max_pages=1,fetcher=lambda url:(body,url),parser=telegram_html_adapter)
        self.assertEqual([p['text'] for p in posts],['Question?'])
        self.assertEqual(status,'partial_telegram_cards')
        self.assertEqual(attempts[0]['rejected_rows']['unparsed_owned_card'],1)
        self.assertEqual(checkpoint['gaps'],[[10,7]])
        later=b'<div class="tgme_widget_message" data-post="newspress/10"><div class="tgme_widget_message_text">New head</div></div>'
        recovered=body.replace(b'newspress/9"></div>',b'newspress/9"><div class="tgme_widget_message_text">Recovered</div></div>')
        with patch('cloud_social.require_cloud'):
            posts,_,_,state=collect(src,checkpoint,max_pages=2,live=True,pause=lambda _:None,
                fetcher=lambda url:(recovered if 'before=10' in url else later,url),parser=telegram_html_adapter)
        self.assertEqual(state['gaps'],[])
        self.assertEqual({p['canonical_key'] for p in posts},{'telegram:newspress/'+str(i) for i in (8,9,10)})

    def test_failed_feeds_cannot_monopolize_next_validation_budget(self):
        class Unavailable(PublicReader):
            def read(self,url):
                if url==SITE:
                    body='<head>'+''.join(f'<link rel="alternate" type="application/rss+xml" href="/feed{i}">' for i in range(8))+'</head>'
                    self.pages+=1
                    return body.encode(),url
                self.pages+=1
                raise ValueError('unverified_feed')
        positions={}
        with patch('source_discovery.DIRECTORIES',{}):
            first,_,_=scan([SOURCE],validation_cursors=positions,reader=Unavailable)
            second,_,_=scan([SOURCE],validation_cursors=positions,reader=Unavailable)
        checked={r['url'] for r in first+second if r.get('reason')=='unverified_feed'}
        self.assertEqual(len(checked),8)

    def test_publisher_feed_retains_routable_www_alias(self):
        a,=publisher_links(b'<head><link rel="alternate" type="application/rss+xml" href="/rss"></head>',
                           'https://www.press.test/',STAMP)
        b,=publisher_links(b'<head><link rel="alternate" type="application/rss+xml" href="/rss"></head>',SITE,STAMP)
        self.assertEqual(a['url'],'https://www.press.test/rss')
        self.assertEqual(a['id'],b['id'])
        self.assertEqual(len(merge_candidates([a],[b])),1)

    def test_review_capacity_does_not_displace_existing_or_verified_sources(self):
        from source_discovery import moderation_queue
        prior=candidate('telegram','https://t.me/publisher','publisher_link',SITE,STAMP)
        lead=candidate('x','https://x.com/news','publisher_link',SITE,STAMP)
        verified={**candidate('rss',FEED,'publisher_feed',SITE,STAMP),'status':'confirmed'}
        rows,deferred=moderation_queue([prior],[lead,prior,verified],limit=1)
        self.assertEqual({r['id'] for r in rows},{prior['id'],verified['id']})
        self.assertEqual(deferred,{'x':1})

    def test_server_retry_after_and_distinct_feed_tick(self):
        holds={}
        note_cooldown(holds,'press.test',HTTPError(SITE,429,'',{'Retry-After':'7200'},None),STAMP)
        self.assertEqual(holds['press.test'],'2026-11-03T12:00:00+00:00')
        self.assertFalse(recent_live_plan({'live_plans':{'feeds':STAMP}},
                                         datetime(2026,11,3,10,1,tzinfo=timezone.utc),'feeds'))


def telegram_html_adapter(source,body,limit):
    return telegram_html(source,limit,body)
