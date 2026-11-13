import unittest
from engagement_metrics import counters, displayed_count
from run import telegram_html
from record_quality import merge_observations
from youtube_feed import parse
from test_youtube_feed import feed, OWNER

class EngagementTests(unittest.TestCase):
    def test_telegram_public_counters_and_missing_are_distinct(self):
        source={'platform':'telegram','id':'example','url':'https://t.me/example'}
        card='<div class="tgme_widget_message" data-post="example/12"><div class="tgme_widget_message_text">Public post</div>{}</div>'
        body=card.format('<span class="tgme_widget_message_views">1.2K</span><a class="tgme_widget_message_replies">12 comments</a><span class="tgme_widget_message_reaction"><i>👍</i>3</span>').encode()
        self.assertEqual(telegram_html(source,10,body)[0]['engagement'],{'views':1200,'comments':12,'likes':3})
        self.assertNotIn('engagement',telegram_html(source,10,card.format('').encode())[0])
        self.assertEqual(displayed_count('0'),0)
        self.assertEqual(displayed_count('1,234'),1234)
        self.assertIsNone(displayed_count('no views'))
        compact=card.format('<span class="tgme_reaction">❤33</span><span class="tgme_reaction">🔥<span class="tgme_reaction_count">2</span></span><span class="tgme_widget_message_replies_text">View 4 comments</span>').encode()
        self.assertEqual(telegram_html(source,10,compact)[0]['engagement'],{'likes':35,'comments':4})

    def test_public_x_embed_counters_stay_on_verified_author(self):
        import json
        from public_embeds import parse_x
        row={'id_str':'123','user':{'screen_name':'example'},'full_text':'Post','favorite_count':4,'reply_count':2,'retweet_count':3}
        data={'props':{'pageProps':{'timeline':{'entries':[{'content':{'tweet':row}}]}}}}
        body=('<script id="__NEXT_DATA__">'+json.dumps(data)+'</script>').encode()
        result=parse_x(body,{'platform':'x','url':'https://x.com/example'},10)[0]
        self.assertEqual(result['engagement'],{'views':None,'likes':4,'comments':2,'shares':3})

    def test_aliases_zero_and_invalid_counts(self):
        self.assertEqual(counters({'engagement':{'playCount':10,'likeCount':0,'reply_count':2,'shareCount':4}}),{'views':10,'likes':0,'comments':2,'shares':4})
        self.assertEqual(counters({'views':True,'likes':-1,'comments_count':'3'}),{'views':None,'likes':None,'comments':None,'shares':None})

    def test_sparse_refresh_preserves_counters_but_accepts_real_zero(self):
        old={'observed_at':'2026-11-01T00:00:00Z','views':10,'engagement':{'likes':2,'comments':3}}
        new={'observed_at':'2026-11-02T00:00:00Z','engagement':{'views':15,'likes':0,'comments':None}}
        merged=merge_observations(old,new)
        self.assertEqual(counters(merged),{'views':15,'likes':0,'comments':3,'shares':None})

    def test_youtube_atom_views_are_not_star_rating_likes(self):
        body=feed().replace(b'<feed ',b'<feed xmlns:media="http://search.yahoo.com/mrss/" ').replace(b'</entry>',b'<media:group><media:community><media:statistics views="1234"/><media:starRating count="8" average="4.5"/></media:community></media:group></entry>')
        row=parse({'platform':'youtube','id':'fixture','url':'https://www.youtube.com/channel/'+OWNER},body,OWNER,10)[0]
        self.assertEqual(row['engagement'],{'views':1234})

if __name__=='__main__': unittest.main()
