"""Anonymous TikTok and X workers, invoked inside the bounded Linux subprocess."""
import asyncio
from itertools import islice
import re
from urllib.parse import urlsplit

from remaining_http import StopCollection


def tiktok_row(video, username, requested_id):
    author = (video.get('author') or {}).get('uniqueId', '')
    video_id = str(video.get('id', ''))
    if author.casefold() != username.casefold() or not re.fullmatch(r'\d+', video_id):
        return None
    if requested_id and video_id != requested_id:
        return None
    from engagement_metrics import retain_counters
    return retain_counters({'url': f'https://www.tiktok.com/@{author}/video/{video_id}', 'source_owner': author,
            'text': video.get('desc'), 'published': video.get('createTime'),
            'media': [(video.get('video') or {}).get('cover')]},video)


async def tiktok(username, video_id, limit):
    from TikTokApi import TikTokApi
    from TikTokApi.exceptions import CaptchaException, EmptyResponseException, NotFoundException
    rows = []
    try:
        async with TikTokApi() as api:
            await api.create_sessions(num_sessions=1, browser='chromium', headless=True, sleep_after=2)
            if video_id:
                url = f'https://www.tiktok.com/@{username}/video/{video_id}'
                row = tiktok_row(await api.video(url=url).info(), username, video_id)
                return [row] if row else []
            async for video in api.user(username=username).videos(count=limit, retries=1):
                row = tiktok_row(video.as_dict, username, None)
                if row:
                    rows.append(row)
                if len(rows) >= limit:
                    break
    except CaptchaException:
        raise StopCollection('challenge_required')
    except EmptyResponseException:
        raise StopCollection('empty_response')
    except NotFoundException:
        raise StopCollection('unavailable')
    return rows


def check_response(response, **kwargs):
    if response.status_code in (401, 403, 429):
        raise StopCollection('http_' + str(response.status_code))
    if urlsplit(response.url).path.startswith(('/i/flow/login', '/account/access')):
        raise StopCollection('login_required')


def tweets(username, tweet_id, limit):
    from snscrape.modules.twitter import TwitterTweetScraper, TwitterUserScraper
    scraper = TwitterTweetScraper(int(tweet_id), retries=0) if tweet_id else TwitterUserScraper(username, retries=0)
    scraper._session.hooks['response'].append(check_response)
    rows = []
    with scraper._session:
        for tweet in islice(scraper.get_items(), limit):
            author = getattr(getattr(tweet, 'user', None), 'username', '')
            if author.casefold() != username.casefold() or (tweet_id and str(tweet.id) != tweet_id):
                continue
            rows.append({'url': f'https://x.com/{author}/status/{tweet.id}', 'source_owner': author,
                         'text': tweet.rawContent, 'published': tweet.date.isoformat(),
                         'engagement':{'likes':getattr(tweet,'likeCount',None),'comments':getattr(tweet,'replyCount',None),
                                       'shares':getattr(tweet,'retweetCount',None),'views':getattr(tweet,'viewCount',None)},
                         'media': [getattr(m, 'fullUrl', None) or getattr(m, 'thumbnailUrl', None)
                                   for m in tweet.media or []]})
    return rows


def read(tool, url, limit):
    path = urlsplit(url).path
    if tool == 'TikTok-Api':
        match = re.fullmatch(r'/@([A-Za-z0-9_.-]{1,64})(?:/video/(\d+))?/?', path)
        if match:
            return asyncio.run(tiktok(*match.groups(), limit))
    elif tool == 'snscrape':
        match = re.fullmatch(r'/([A-Za-z0-9_]{1,15})(?:/status/(\d+))?/?', path)
        if match and match[1].lower() not in {'home', 'search', 'explore', 'intent', 'i'}:
            return tweets(*match.groups(), limit)
    raise StopCollection('unsupported_source_shape')
