#include "PlatformAdapters.h"

#include <iostream>
#include <cstdlib>
#include <string>

namespace {
int failures = 0;
void expect(bool condition, const char *message)
{
    if (!condition)
    {
        std::cerr << "FAIL: " << message << '\n';
        ++failures;
    }
}
}

int main()
{
    using namespace argus::collectors;

    const auto youtube = parseYouTubeFeed(
        R"(<feed><entry><yt:videoId>abc-123_X</yt:videoId><title>Title</title><published>2026-07-23T10:00:00Z</published><updated>2026-07-23T10:05:00Z</updated></entry></feed>)", 10);
    expect(youtube.size() == 1, "youtube entry parsed");
    expect(youtube[0].canonicalKey == "youtube:abc-123_X", "youtube stable key");
    expect(youtube[0].postUrl == "https://www.youtube.com/watch?v=abc-123_X", "youtube watch URL");
    expect(youtube[0].updatedAt == "2026-07-23T10:05:00Z", "youtube updated timestamp");

    const std::string youtubeChannelId = "UC_x5XG1OV2P6uZZ5FSM9Ttw";
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/@YouTube",
               R"html(<link href="https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw" rel="canonical">)html") ==
               youtubeChannelId,
           "youtube handle resolves canonical channel link regardless of attribute order");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/c/YouTube",
               R"json({"metadata":{"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"}})json") ==
               youtubeChannelId,
           "youtube legacy custom path resolves externalId");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/user/YouTube",
               R"html(<meta content="UC_x5XG1OV2P6uZZ5FSM9Ttw" itemprop="channelId">)html") ==
               youtubeChannelId,
           "youtube legacy user path resolves channel-owned metadata");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/LegacyVanity",
               R"html(<link rel="canonical" href="https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw">)html") ==
               youtubeChannelId,
           "youtube legacy vanity path resolves only a canonical channel link");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/LegacyVanity",
               R"json({"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"})json").empty(),
           "youtube legacy vanity path rejects an unscoped externalId");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/@requested",
               R"json({"videoRenderer":{"channelId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"}})json").empty(),
           "youtube recommendation channelId cannot be cached as source owner");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw") == youtubeChannelId,
           "youtube direct channel path resolves without HTML");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw") ==
               youtubeChannelId,
           "youtube feed query resolves without HTML");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/watch?v=video",
               R"html(<link rel="canonical" href="https://www.youtube.com/watch?v=video"><script>{"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"}</script>)html").empty(),
           "youtube watch page cannot become a channel source");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/playlist",
               R"json({"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"})json").empty(),
           "youtube reserved single-segment path cannot become a channel source");
    expect(youtubeChannelIdFromSource(
               "https://www.youtube.com/@missing",
               R"json({"browseId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"})json").empty(),
           "youtube recommendation-only browseId is rejected");
    expect(youtubeChannelIdFromSource(
               "https://evil.example/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw").empty(),
           "youtube channel ID on a foreign host is rejected");
    expect(youtubeChannelIdFromSource(
               "https://youtu.be/video",
               R"json({"externalId":"UC_x5XG1OV2P6uZZ5FSM9Ttw"})json").empty(),
           "youtube short video URL is not treated as a channel source");

    const auto telegram = parseTelegramHtml(
        R"html(<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="channel_name/42" data-edit-date="1753269000"><div class="tgme_widget_message_text">Hello<br>world</div><a style="background-image:url('https://img.example/a.jpg')"></a><video src="https://media.example/v.mp4"></video><a class="tgme_widget_message_date"><time datetime="2026-07-23T11:00:00+00:00"></time></a></div></div></section>)html",
        "channel_name", 10);
    expect(telegram.size() == 1, "telegram message parsed");
    expect(telegram[0].canonicalKey == "telegram:channel_name:42", "telegram stable key");
    expect(!telegram[0].updatedAt.empty(), "telegram edit timestamp");
    expect(telegram[0].images.size() == 1, "telegram image metadata");
    expect(telegram[0].videos.size() == 1, "telegram video metadata");

    const auto reddit = parseRedditJson(
        R"({"data":{"children":[{"data":{"name":"t3_xyz","id":"xyz","title":"News","selftext":"Body","permalink":"/r/test/comments/xyz/news/","created_utc":1753268400,"thumbnail":"https://img.example/x.jpg"}}]}})", 10);
    expect(reddit.size() == 1, "reddit listing parsed");
    expect(reddit[0].canonicalKey == "reddit:t3_xyz", "reddit fullname key");
    const auto redditAtom = parseRedditAtom(
        R"xml(<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_AbC123</id><link href="https://www.reddit.com/r/worldnews/comments/abc123/example/?utm_source=rss"/><title>Example &amp; update</title><content type="html">&lt;p&gt;Entry body&lt;/p&gt;</content><updated>2026-07-24T09:30:00Z</updated></entry></feed>)xml",
        10);
    expect(redditAtom.size() == 1, "reddit public Atom entry parsed");
    expect(redditAtom[0].canonicalKey == "reddit:t3_abc123", "reddit Atom fullname is idempotent");
    expect(redditAtom[0].postUrl.find("/comments/abc123/") != std::string::npos,
           "reddit Atom post URL retained");
    expect(redditAtom[0].text == "Example & update\n\nEntry body", "reddit Atom content decoded");
    expect(redditAtom[0].publishedAt == "2026-07-24T09:30:00Z" &&
               redditAtom[0].updatedAt == "2026-07-24T09:30:00Z",
           "reddit Atom updated timestamp mapped");
    const auto redditAtomLinkFallback = parseRedditAtom(
        R"xml(<feed><entry><id>not-a-fullname</id><link href="https://www.reddit.com/r/test/comments/Z9Y8X7/title/"/><title>Fallback</title><updated>2026-07-24T09:31:00Z</updated></entry></feed>)xml",
        1);
    expect(redditAtomLinkFallback.size() == 1 &&
               redditAtomLinkFallback[0].canonicalKey == "reddit:t3_z9y8x7",
           "reddit Atom identity falls back to comments URL");
    expect(parseRedditAtom(
               R"xml(<feed><entry><id>t3_bad</id><link href="https://evil.example/comments/bad/x"/><title>Bad</title></entry></feed>)xml",
               1).empty(),
           "reddit Atom rejects non-Reddit post URLs");

    const auto vk = parseVkJson(
        R"({"response":{"items":[{"id":7,"owner_id":-99,"date":1753268400,"text":"VK post","attachments":[]}]}})", 10);
    expect(vk.size() == 1, "vk wall parsed");
    expect(vk[0].canonicalKey == "vk:-99:7", "vk owner and post key");

    const auto gdelt = parseGdeltJson(
        R"({"articles":[{"url":"https://news.example/item","title":"GDELT item","seendate":"20260723T120000Z","socialimage":"https://news.example/image.jpg"}]})", 10);
    expect(gdelt.size() == 1, "gdelt article parsed");
    expect(gdelt[0].canonicalKey == "gdelt:https://news.example/item", "gdelt canonical URL key");
    const auto gdeltUrl = buildGdeltUrl(
        "https://api.gdeltproject.org/api/v2/doc/doc?query=ukraine&format=html&timespan=1d",
        1753268400, 1753270200, 999);
    expect(gdeltUrl.find("query=ukraine") != std::string::npos, "gdelt preserves documented query");
    expect(gdeltUrl.find("mode=artlist") != std::string::npos, "gdelt artlist mode");
    expect(gdeltUrl.find("maxrecords=250") != std::string::npos, "gdelt bounded page size");
    expect(gdeltUrl.find("startdatetime=20250723104500") != std::string::npos,
           "gdelt late-arrival overlap");
    expect(gdeltUrl.find("enddatetime=20250723113000") != std::string::npos,
           "gdelt cursor end");

    expect(challengePage("<title>Just a moment...</title><div class=cf-turnstile>"),
           "dsns Cloudflare challenge classified");
    expect(!challengePage("<rss><channel><item>safe</item></channel></rss>"),
           "dsns feed is not a challenge");

    const auto dsnsHtml = parseDsnsHtml(
        R"html(<article><time datetime="2026-07-24T08:00:00Z"></time><img src="/images/a.jpg"><a href="/news/ostanni-novini/test-item?utm_source=x"><strong>ДСНС</strong> повідомляє</a></article>)html",
        "https://fixture.invalid/source/43", 10);
    expect(dsnsHtml.size() == 1, "dsns Joomla HTML item parsed");
    expect(dsnsHtml[0].canonicalKey.rfind("dsns-http:https://fixture.invalid/source/43news/", 0) == 0,
           "dsns stable canonical URL key");
    expect(dsnsHtml[0].postUrl.find('?') == std::string::npos, "dsns tracking query removed");
    expect(dsnsHtml[0].publishedAt == "2026-07-24T08:00:00Z", "dsns HTML timestamp");

    if (failures == 0) std::cout << "platform adapter tests passed\n";
    return failures == 0 ? 0 : 1;
}
