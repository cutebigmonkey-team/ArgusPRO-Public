#include "TelevisionController.h"

#include <drogon/drogon.h>
#include <curl/curl.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

// Real backend contract for the Television tab — replaces the reference
// app's `/api/posts?platform=youtube&source_id=...` (which reads from a
// Postgres collector pipeline this phase intentionally doesn't have; see
// project constraints on not connecting a real DB/collectors/AI).
//
// Instead of falling back to a static fixture, this proxies YouTube's own
// public per-channel Atom feed (https://www.youtube.com/feeds/videos.xml,
// no API key, no OAuth, no secret of any kind — it's the same feed any RSS
// reader can subscribe to) so the tab shows genuinely current, genuinely
// playable video IDs for the same 7 real Ukrainian channels the reference
// app hardcoded. See docs/television.md for the full writeup.
using namespace drogon;

namespace argus::backend::television {

namespace {

struct ChannelMeta
{
    const char *channelId;
    const char *name;
    const char *shortLabel;
    const char *logoColor;  // hex, matches the reference's Tailwind bg-* swatch for this channel
};

// Same 7 channels the reference app's Television.tsx hardcodes (CHANNELS
// const) — real YouTube channel IDs, not fixture data.
constexpr std::array<ChannelMeta, 7> kChannels = {{
    {"UCVEaAWKfv7fE1c-ZuBs7TKQ", "1+1", "1+1", "#ef4444"},
    {"UCkyrSWEcjZKpIwMxiPfOcgg", "5 канал", "5", "#e11d48"},
    {"UC5V8mErVFOpcQXEb3y9IMZw", "Рада", "Ra", "#eab308"},
    {"UCPY6gj8G7dqwPxg9KwHrj5Q", "Перший", "UA", "#1d4ed8"},
    {"UCdubelOloxR3wzwJG9x8YqQ", "Дождь", "TVR", "#334155"},
    {"UCQVtD_N4OeD-9PshBq7NwyQ", "Фейгин Live", "FL", "#4338ca"},
    {"UC3kKhUjae8xVvXRLjHibbeQ", "Н24", "N24", "#0f766e"},
}};

const ChannelMeta *findChannel(const std::string &channelId)
{
    for (const auto &c : kChannels)
        if (channelId == c.channelId) return &c;
    return nullptr;
}

size_t curlWriteCallback(char *ptr, size_t size, size_t nmemb, void *userdata)
{
    auto *buf = static_cast<std::string *>(userdata);
    buf->append(ptr, size * nmemb);
    return size * nmemb;
}

// Blocking libcurl call — must never run on one of Drogon's own IO-loop
// threads (app().setThreadNum(2)): a slow/stalled upstream would occupy an
// IO thread for up to CURLOPT_TIMEOUT seconds, and with only 2 threads
// total, two concurrent slow fetches (e.g. a TV-tab page load firing
// several /api/tv/videos requests at once) starve the entire event loop —
// including trivial handlers like /health — which previously cascaded into
// Postgres client fd exhaustion. Callers must dispatch this on a separate
// thread, never call it directly from a Drogon handler body.
bool httpsGet(const std::string &url, std::string &outBody)
{
    CURL *curl = curl_easy_init();
    if (!curl) return false;
    outBody.clear();
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, curlWriteCallback);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &outBody);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 8L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "ArgusPRO/1.0 (+local-ui; television-tab)");
    const CURLcode res = curl_easy_perform(curl);
    long httpCode = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &httpCode);
    curl_easy_cleanup(curl);
    return res == CURLE_OK && httpCode == 200;
}

std::string xmlUnescape(const std::string &s)
{
    std::string out;
    out.reserve(s.size());
    for (size_t i = 0; i < s.size();)
    {
        if (s.compare(i, 5, "&amp;") == 0) { out += '&'; i += 5; }
        else if (s.compare(i, 4, "&lt;") == 0) { out += '<'; i += 4; }
        else if (s.compare(i, 4, "&gt;") == 0) { out += '>'; i += 4; }
        else if (s.compare(i, 6, "&quot;") == 0) { out += '"'; i += 6; }
        else if (s.compare(i, 6, "&apos;") == 0) { out += '\''; i += 6; }
        else if (s.compare(i, 5, "&#39;") == 0) { out += '\''; i += 5; }
        else { out += s[i]; ++i; }
    }
    return out;
}

// Extracts the content of the first <tag>...</tag> found in `block`. The
// feed format is stable/predictable (verified against a live fetch), so a
// small targeted scan is enough here — pulling in a full XML library for
// one Atom feed shape would be over-engineering.
std::string extractTag(const std::string &block, const std::string &tag)
{
    const std::string open = "<" + tag + ">";
    const std::string close = "</" + tag + ">";
    const auto start = block.find(open);
    if (start == std::string::npos) return "";
    const auto contentStart = start + open.size();
    const auto end = block.find(close, contentStart);
    if (end == std::string::npos) return "";
    return xmlUnescape(block.substr(contentStart, end - contentStart));
}

struct VideoEntry
{
    std::string videoId, title, publishedAt;
};

std::vector<VideoEntry> parseFeed(const std::string &xml, size_t limit)
{
    std::vector<VideoEntry> out;
    size_t pos = 0;
    while (out.size() < limit)
    {
        const auto entryStart = xml.find("<entry>", pos);
        if (entryStart == std::string::npos) break;
        const auto entryEnd = xml.find("</entry>", entryStart);
        if (entryEnd == std::string::npos) break;
        const std::string block = xml.substr(entryStart, entryEnd - entryStart);
        pos = entryEnd + 8;

        VideoEntry v;
        v.videoId = extractTag(block, "yt:videoId");
        v.title = extractTag(block, "title");
        v.publishedAt = extractTag(block, "published");
        if (!v.videoId.empty()) out.push_back(std::move(v));
    }
    return out;
}

Json::Value videoEntryToJson(const VideoEntry &e)
{
    Json::Value v;
    v["videoId"] = e.videoId;
    v["title"] = e.title;
    v["publishedAt"] = e.publishedAt;
    v["url"] = "https://www.youtube.com/watch?v=" + e.videoId;
    v["thumbnailUrl"] = "https://i.ytimg.com/vi/" + e.videoId + "/mqdefault.jpg";
    return v;
}

// Tiny in-memory response cache, keyed by channelId — avoids re-fetching
// YouTube's feed on every render/channel-revisit within the TTL window.
// Guarded by a mutex since Drogon's handlers can run on multiple IO threads.
struct CacheEntry
{
    Json::Value body;
    std::chrono::steady_clock::time_point fetchedAt;
};
std::mutex g_cacheMutex;
std::unordered_map<std::string, CacheEntry> g_cache;
constexpr auto kCacheTtl = std::chrono::minutes(5);

}  // namespace

void registerRoutes()
{
    app().registerHandler(
        "/api/tv/channels",
        [](const HttpRequestPtr &, std::function<void(const HttpResponsePtr &)> &&callback) {
            Json::Value list(Json::arrayValue);
            for (const auto &c : kChannels)
            {
                Json::Value ch;
                ch["channelId"] = c.channelId;
                ch["name"] = c.name;
                ch["shortLabel"] = c.shortLabel;
                ch["logoColor"] = c.logoColor;
                list.append(ch);
            }
            Json::Value json;
            json["channels"] = list;
            callback(HttpResponse::newHttpJsonResponse(json));
        },
        {Get});

    app().registerHandler(
        "/api/tv/videos",
        [](const HttpRequestPtr &req, std::function<void(const HttpResponsePtr &)> &&callback) {
            const std::string channelId = req->getParameter("channelId");

            size_t limit = 20;
            if (const std::string limitParam = req->getParameter("limit"); !limitParam.empty())
            {
                try
                {
                    limit = static_cast<size_t>(std::clamp(std::stoi(limitParam), 1, 50));
                }
                catch (const std::exception &)
                {
                    limit = 20;
                }
            }

            const ChannelMeta *channel = findChannel(channelId);
            if (channel == nullptr)
            {
                Json::Value err;
                err["error"] = "unknown_channel";
                auto resp = HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(k400BadRequest);
                callback(resp);
                return;
            }

            {
                std::lock_guard<std::mutex> lock(g_cacheMutex);
                const auto it = g_cache.find(channelId);
                if (it != g_cache.end() &&
                    (std::chrono::steady_clock::now() - it->second.fetchedAt) < kCacheTtl)
                {
                    callback(HttpResponse::newHttpJsonResponse(it->second.body));
                    return;
                }
            }

            // The YouTube fetch below blocks on network I/O (curl_easy_perform);
            // it runs on a dedicated, detached thread rather than this Drogon
            // IO-loop thread so a slow/stalled upstream can never starve the
            // event loop (see httpsGet's comment). `callback` is safe to
            // invoke from any thread.
            const std::string feedUrl = "https://www.youtube.com/feeds/videos.xml?channel_id=" + channelId;
            std::thread([channelId, feedUrl, limit, callback = std::move(callback)]() mutable {
                std::string xml;
                if (!httpsGet(feedUrl, xml))
                {
                    Json::Value err;
                    err["error"] = "upstream_unavailable";
                    auto resp = HttpResponse::newHttpJsonResponse(err);
                    resp->setStatusCode(k502BadGateway);
                    callback(resp);
                    return;
                }

                const auto entries = parseFeed(xml, limit);

                Json::Value videos(Json::arrayValue);
                for (const auto &e : entries) videos.append(videoEntryToJson(e));

                Json::Value json;
                json["channelId"] = channelId;
                json["videos"] = videos;
                json["total"] = static_cast<int>(entries.size());

                {
                    std::lock_guard<std::mutex> lock(g_cacheMutex);
                    g_cache[channelId] = CacheEntry{json, std::chrono::steady_clock::now()};
                }

                callback(HttpResponse::newHttpJsonResponse(json));
            }).detach();
        },
        {Get});
}

}  // namespace argus::backend::television
