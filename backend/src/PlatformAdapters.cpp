#include "PlatformAdapters.h"

#include "PipelineDb.h"
#include "RssNormalize.h"

#include <curl/curl.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <ctime>
#include <memory>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <unordered_map>
#include <utility>

namespace argus::collectors {
namespace {

struct Download {
    std::string body;
    std::string effectiveUrl;
    long status{0};
    bool tooLarge{false};
};

struct WriteState {
    std::string *body;
    std::size_t maximum;
    bool *tooLarge;
};

int abortWhenStopping(void *, curl_off_t, curl_off_t, curl_off_t, curl_off_t)
{
    return argus::pipeline::stopping() ? 1 : 0;
}

size_t writeBody(char *data, size_t size, size_t count, void *opaque)
{
    auto &state = *static_cast<WriteState *>(opaque);
    const size_t bytes = size * count;
    if (bytes > state.maximum - std::min(state.maximum, state.body->size()))
    {
        *state.tooLarge = true;
        return 0;
    }
    state.body->append(data, bytes);
    return bytes;
}

std::string lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

std::string hostOf(const std::string &url)
{
    CURLU *parsed = curl_url();
    if (!parsed) return {};
    std::string host;
    if (curl_url_set(parsed, CURLUPART_URL, url.c_str(), 0) == CURLUE_OK)
    {
        char *raw = nullptr;
        if (curl_url_get(parsed, CURLUPART_HOST, &raw, 0) == CURLUE_OK && raw)
        {
            host = lower(raw);
            curl_free(raw);
        }
    }
    curl_url_cleanup(parsed);
    return host;
}

std::string pathOf(const std::string &url)
{
    CURLU *parsed = curl_url();
    if (!parsed) return {};
    std::string path;
    if (curl_url_set(parsed, CURLUPART_URL, url.c_str(), 0) == CURLUE_OK)
    {
        char *raw = nullptr;
        if (curl_url_get(parsed, CURLUPART_PATH, &raw, 0) == CURLUE_OK && raw)
        {
            path = raw;
            curl_free(raw);
        }
    }
    curl_url_cleanup(parsed);
    return path;
}

bool matchesHost(const std::string &host, const std::vector<std::string> &allowed)
{
    for (const auto &candidate : allowed)
    {
        if (host == candidate) return true;
        if (!candidate.empty() && candidate.front() == '.' && host.size() > candidate.size() &&
            host.ends_with(candidate)) return true;
    }
    return false;
}

Download performRequest(const std::string &url, const std::vector<std::string> &allowed,
                        const HttpOptions &options, const std::vector<std::string> &headers,
                        const std::string &userAgent, const std::string &postBody = {},
                        const std::string &userPassword = {})
{
    if (!matchesHost(hostOf(url), allowed)) throw std::runtime_error("hostname_not_allowed");
    CURL *curl = curl_easy_init();
    if (!curl) throw std::runtime_error("http_init_failed");
    Download result;
    WriteState state{&result.body, options.maxBytes, &result.tooLarge};
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeBody);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &state);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_MAXREDIRS, 4L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, options.connectTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, options.totalTimeoutSeconds);
    curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "https");
    curl_easy_setopt(curl, CURLOPT_REDIR_PROTOCOLS_STR, "https");
    curl_easy_setopt(curl, CURLOPT_USERAGENT, userAgent.c_str());
    curl_easy_setopt(curl, CURLOPT_ACCEPT_ENCODING, "");
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, abortWhenStopping);
    if (!postBody.empty())
    {
        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDS, postBody.c_str());
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, static_cast<long>(postBody.size()));
    }
    if (!userPassword.empty())
    {
        curl_easy_setopt(curl, CURLOPT_HTTPAUTH, CURLAUTH_BASIC);
        curl_easy_setopt(curl, CURLOPT_USERPWD, userPassword.c_str());
    }
    curl_slist *list = nullptr;
    for (const auto &header : headers) list = curl_slist_append(list, header.c_str());
    if (list) curl_easy_setopt(curl, CURLOPT_HTTPHEADER, list);
    const CURLcode code = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result.status);
    char *effective = nullptr;
    curl_easy_getinfo(curl, CURLINFO_EFFECTIVE_URL, &effective);
    if (effective) result.effectiveUrl = effective;
    if (list) curl_slist_free_all(list);
    curl_easy_cleanup(curl);
    if (result.tooLarge) throw std::runtime_error("response_too_large");
    if (code != CURLE_OK)
    {
        if (code == CURLE_ABORTED_BY_CALLBACK && argus::pipeline::stopping())
            throw std::runtime_error("collection_stopped");
        throw std::runtime_error(code == CURLE_OPERATION_TIMEDOUT ? "http_timeout" : "http_failed");
    }
    if (!matchesHost(hostOf(result.effectiveUrl), allowed)) throw std::runtime_error("redirect_hostname_not_allowed");
    if (result.status == 429) throw std::runtime_error("rate_limited");
    if (result.status < 200 || result.status >= 300) throw std::runtime_error("http_status_failed");
    return result;
}

Download get(const std::string &url, const std::vector<std::string> &allowed,
             const HttpOptions &options, const std::vector<std::string> &headers = {},
             const std::string &userAgent = "ArgusPRO-ShadowCollector/1.0 (+https://argusnews.online)")
{
    return performRequest(url, allowed, options, headers, userAgent);
}

Download postForm(const std::string &url, const std::vector<std::string> &allowed,
                  const HttpOptions &options, const std::string &body,
                  const std::string &userPassword, const std::string &userAgent)
{
    return performRequest(url, allowed, options,
                          {"Accept: application/json",
                           "Content-Type: application/x-www-form-urlencoded"},
                          userAgent, body, userPassword);
}

std::string xml(const std::string &block, const std::string &tag)
{
    return argus::rss::tag(block, tag);
}

std::string decodeHtml(std::string value)
{
    value = std::regex_replace(value, std::regex("<br\\s*/?>", std::regex::icase), "\n");
    value = std::regex_replace(value, std::regex("<[^>]+>"), " ");
    value = argus::rss::unescape(std::move(value));
    value = std::regex_replace(value, std::regex("[ \\t\\r\\n]+"), " ");
    if (!value.empty() && value.front() == ' ') value.erase(value.begin());
    if (!value.empty() && value.back() == ' ') value.pop_back();
    return value;
}

std::optional<std::string> capture(const std::string &value, const std::regex &pattern,
                                   std::size_t group = 1)
{
    std::smatch match;
    if (std::regex_search(value, match, pattern) && group < match.size()) return match[group].str();
    return std::nullopt;
}

Json::Value parseJson(const std::string &text)
{
    Json::CharReaderBuilder builder;
    Json::Value root;
    std::string errors;
    const std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
    if (!reader->parse(text.data(), text.data() + text.size(), &root, &errors))
        throw std::runtime_error("invalid_json");
    return root;
}

std::string urlEncode(const std::string &value)
{
    CURL *curl = curl_easy_init();
    if (!curl) return {};
    char *encoded = curl_easy_escape(curl, value.c_str(), static_cast<int>(value.size()));
    const std::string result = encoded ? encoded : "";
    curl_free(encoded);
    curl_easy_cleanup(curl);
    return result;
}

std::string isoTimestamp(std::time_t raw)
{
    char formatted[32]{};
    const std::tm *utc = std::gmtime(&raw);
    if (!utc || std::strftime(formatted, sizeof(formatted), "%Y-%m-%dT%H:%M:%SZ", utc) == 0)
        return {};
    return formatted;
}

std::string gdeltTimestamp(std::time_t raw)
{
    char formatted[24]{};
    const std::tm *utc = std::gmtime(&raw);
    if (!utc || std::strftime(formatted, sizeof(formatted), "%Y%m%d%H%M%S", utc) == 0)
        return {};
    return formatted;
}

bool isGdeltControlParameter(const std::string &part)
{
    const std::string name = lower(part.substr(0, part.find('=')));
    return name == "mode" || name == "format" || name == "maxrecords" ||
           name == "sort" || name == "startdatetime" || name == "enddatetime" ||
           name == "timespan";
}

bool isYouTubeHost(const std::string &url)
{
    const std::string host = hostOf(url);
    return host == "youtube.com" || host == "www.youtube.com";
}

bool isYouTubeChannelPageUrl(const std::string &url)
{
    if (!isYouTubeHost(url)) return false;
    static const std::regex pathPattern(
        R"(^/(?:@[^/]+|(?:c|user)/[^/]+)(?:/(?:featured|videos|shorts|streams|playlists|community|about))?/?$)",
        std::regex::icase);
    return std::regex_match(pathOf(url), pathPattern);
}

bool isYouTubeLegacyVanityPageUrl(const std::string &url)
{
    if (!isYouTubeHost(url)) return false;
    const std::string path = pathOf(url);
    if (!std::regex_match(path, std::regex(R"(^/[A-Za-z0-9._~-]+/?$)"))) return false;
    std::string slug = path.substr(1);
    if (!slug.empty() && slug.back() == '/') slug.pop_back();
    static const std::set<std::string> reserved{
        "account", "channel", "create_channel", "embed", "feed", "feeds", "gaming",
        "history", "kids", "live", "logout", "music", "playlist", "premium", "results",
        "shorts", "signin", "subscription_manager", "upload", "user", "watch"};
    return !reserved.contains(lower(std::move(slug)));
}

std::string normalizedYouTubePageUrl(const std::string &url)
{
    if (!isYouTubeChannelPageUrl(url) && !isYouTubeLegacyVanityPageUrl(url)) return {};
    const std::string lowered = lower(url.substr(0, std::min<std::size_t>(url.size(), 8)));
    if (lowered.starts_with("https://")) return url;
    if (lowered.starts_with("http://")) return "https://" + url.substr(7);
    return {};
}

std::string channelFromYouTube(const std::string &url, const HttpOptions &options)
{
    static std::unordered_map<std::string, std::string> cache;
    if (const std::string direct = youtubeChannelIdFromSource(url); !direct.empty()) return direct;
    if (const auto it = cache.find(url); it != cache.end()) return it->second;
    const std::string pageUrl = normalizedYouTubePageUrl(url);
    if (pageUrl.empty()) return {};
    const auto page = get(pageUrl, {"youtube.com", "www.youtube.com"}, options);
    const std::string id = youtubeChannelIdFromSource(page.effectiveUrl, page.body);
    if (id.empty()) return {};
    cache[url] = id;
    return id;
}

std::string telegramChannel(const std::string &url)
{
    if (const auto value = capture(url, std::regex(R"(t(?:elegram)?\.me/(?:s/)?([A-Za-z0-9_]{4,64}))", std::regex::icase)))
        return *value;
    if (const auto value = capture(url, std::regex(R"(^@?([A-Za-z0-9_]{4,64})$)"))) return *value;
    return {};
}

std::string redditCommunity(const std::string &url)
{
    if (const auto value = capture(url, std::regex(R"(reddit\.com/r/([A-Za-z0-9_]+))", std::regex::icase)))
        return *value;
    return {};
}

std::string redditAccessToken(const std::string &clientId, const std::string &clientSecret,
                              const HttpOptions &options)
{
    static std::string cachedToken;
    static std::time_t expiresAt{0};
    const std::time_t now = std::time(nullptr);
    if (!cachedToken.empty() && expiresAt > now + 60) return cachedToken;
    const auto response = postForm(
        "https://www.reddit.com/api/v1/access_token", {"www.reddit.com"}, options,
        "grant_type=client_credentials", clientId + ":" + clientSecret,
        "web:argus-dashboard:1.0 (by /u/argus_dashboard_bot)");
    const Json::Value token = parseJson(response.body);
    cachedToken = token.get("access_token", "").asString();
    if (cachedToken.empty()) throw std::runtime_error("auth_required");
    expiresAt = now + std::max(60, token.get("expires_in", 3600).asInt() - 60);
    return cachedToken;
}

std::string vkSlug(const std::string &url)
{
    if (const auto value = capture(url, std::regex(R"(vk\.com/([^/?#]+))", std::regex::icase))) return *value;
    return {};
}

Result collectDsns(const std::string &sourceUrl, std::size_t limit, const HttpOptions &options)
{
    const std::vector<std::string> hosts{"dsns.gov.ua", ".dsns.gov.ua"};
    const std::vector<std::string> paths{"", "/rss", "/rss.xml", "/feed", "/feed.xml",
                                          "/?format=feed&type=rss", "/index.php?format=feed&type=rss"};
    for (const auto &path : paths)
    {
        if (argus::pipeline::stopping()) return {{}, "collection_stopped"};
        try
        {
            const std::string candidate = path.empty() ? sourceUrl : argus::rss::resolveUrl(sourceUrl, path);
            const auto response = get(candidate, hosts, options);
            if (challengePage(response.body)) return {{}, "challenge_required"};
            if (!argus::rss::isFeed(response.body))
            {
                if (path.empty())
                {
                    auto htmlItems = parseDsnsHtml(response.body, response.effectiveUrl, limit);
                    if (!htmlItems.empty()) return {std::move(htmlItems), {}};
                }
                continue;
            }
            std::vector<Item> output;
            for (const auto &entry : argus::rss::parse(response.body, limit))
            {
                const std::string link = argus::rss::canonicalize(argus::rss::resolveUrl(response.effectiveUrl, entry.link));
                if (!matchesHost(hostOf(link), hosts)) continue;
                Item item;
                item.canonicalKey = "dsns-http:" + link;
                item.postUrl = link;
                item.text = entry.title;
                item.publishedAt = entry.published;
                output.push_back(std::move(item));
            }
            if (!output.empty()) return {std::move(output), {}};
        }
        catch (const std::runtime_error &error)
        {
            if (std::string(error.what()) == "rate_limited") return {{}, "rate_limited"};
        }
    }
    return {{}, "feed_not_found"};
}

}  // namespace

std::string youtubeChannelIdFromSource(const std::string &sourceUrl,
                                       const std::string &html)
{
    if (!isYouTubeHost(sourceUrl)) return {};
    if (const auto direct = capture(
            pathOf(sourceUrl),
            std::regex(R"(^/channel/(UC[A-Za-z0-9_-]{10,})(?:/|$))", std::regex::icase)))
        return *direct;
    if (const auto parameter = capture(
            sourceUrl, std::regex(R"([?&]channel_id=(UC[A-Za-z0-9_-]{10,}))", std::regex::icase)))
        return *parameter;
    const bool legacyVanity = isYouTubeLegacyVanityPageUrl(sourceUrl);
    if ((!isYouTubeChannelPageUrl(sourceUrl) && !legacyVanity) || html.empty()) return {};

    // A channel-owned canonical link is the strongest modern marker. It is
    // deliberately scoped to <link rel=canonical>, so a recommended video or
    // unrelated browseId cannot be mistaken for the requested channel.
    const std::regex canonicalTag(
        R"re(<link\b[^>]*\brel\s*=\s*["']canonical["'][^>]*>)re",
        std::regex::icase);
    for (std::sregex_iterator it(html.begin(), html.end(), canonicalTag), end; it != end; ++it)
        if (const auto id = capture(
                it->str(), std::regex(R"(/channel/(UC[A-Za-z0-9_-]{10,}))", std::regex::icase)))
            return *id;

    const std::regex channelMeta(
        R"re(<meta\b[^>]*\bitemprop\s*=\s*["']channelId["'][^>]*>)re",
        std::regex::icase);
    for (std::sregex_iterator it(html.begin(), html.end(), channelMeta), end; it != end; ++it)
        if (const auto id = capture(it->str(), std::regex(R"((UC[A-Za-z0-9_-]{10,}))")))
            return *id;

    // Current YouTube channel-owner bootstrap data uses externalId. Do not
    // fall back to a generic channelId anywhere in the document: channel
    // pages also embed recommended video renderers owned by other channels.
    if (!legacyVanity)
        if (const auto external = capture(
                html, std::regex(R"re("externalId"\s*:\s*"(UC[A-Za-z0-9_-]{10,})")re")))
            return *external;
    return {};
}

bool challengePage(const std::string &body)
{
    const std::string sample = lower(body.substr(0, std::min<std::size_t>(body.size(), 128 * 1024)));
    return sample.find("cf-turnstile") != std::string::npos ||
           sample.find("challenge-platform") != std::string::npos ||
           sample.find("just a moment...") != std::string::npos ||
           sample.find("captcha") != std::string::npos;
}

std::vector<Item> parseYouTubeFeed(const std::string &body, std::size_t limit)
{
    std::vector<Item> output;
    std::size_t cursor = 0;
    while (output.size() < limit)
    {
        const auto begin = body.find("<entry", cursor);
        if (begin == std::string::npos) break;
        const auto end = body.find("</entry>", begin);
        if (end == std::string::npos) break;
        const std::string block = body.substr(begin, end - begin);
        const std::string id = xml(block, "yt:videoId");
        if (!id.empty())
        {
            Item item;
            item.canonicalKey = "youtube:" + id;
            item.postUrl = "https://www.youtube.com/watch?v=" + id;
            item.text = xml(block, "title");
            item.publishedAt = xml(block, "published");
            item.updatedAt = xml(block, "updated");
            const std::string thumbnail = "https://i.ytimg.com/vi/" + id + "/mqdefault.jpg";
            item.images.append(thumbnail);
            output.push_back(std::move(item));
        }
        cursor = end + 8;
    }
    return output;
}

std::vector<Item> parseTelegramHtml(const std::string &html, const std::string &channel,
                                    std::size_t limit)
{
    std::vector<Item> output;
    const std::regex blockPattern(R"(<div class="tgme_widget_message_wrap[\s\S]*?(?=<div class="tgme_widget_message_wrap|</section>|</main>|$))");
    for (std::sregex_iterator it(html.begin(), html.end(), blockPattern), end; it != end && output.size() < limit; ++it)
    {
        const std::string block = it->str();
        const auto messageId = capture(block, std::regex(R"re(data-post="([^"/]+)/([0-9]+)")re"), 2);
        if (!messageId) continue;
        Item item;
        item.canonicalKey = "telegram:" + channel + ":" + *messageId;
        item.postUrl = "https://t.me/" + channel + "/" + *messageId;
        if (const auto text = capture(block, std::regex(R"(<div[^>]*class="tgme_widget_message_text[^"]*"[^>]*>([\s\S]*?)</div>)")))
            item.text = decodeHtml(*text);
        if (const auto timestamp = capture(block, std::regex(R"re(<time[^>]*datetime="([^"]+)")re")))
            item.publishedAt = *timestamp;
        if (const auto edited = capture(block, std::regex(R"re(data-edit-date="([0-9]+)")re")))
            item.updatedAt = isoTimestamp(static_cast<std::time_t>(std::stoll(*edited)));
        const std::regex imagePattern(R"(background-image:url\(['"]?([^'"\)]+))");
        for (std::sregex_iterator image(block.begin(), block.end(), imagePattern), imageEnd;
             image != imageEnd; ++image)
            item.images.append(argus::rss::unescape((*image)[1].str()));
        const std::regex videoPattern(R"re(<video[^>]+(?:src|poster)="([^"]+)")re", std::regex::icase);
        for (std::sregex_iterator video(block.begin(), block.end(), videoPattern), videoEnd;
             video != videoEnd; ++video)
        {
            Json::Value metadata;
            metadata["url"] = argus::rss::unescape((*video)[1].str());
            metadata["kind"] = "telegram_video_metadata";
            item.videos.append(std::move(metadata));
        }
        output.push_back(std::move(item));
    }
    return output;
}

std::vector<Item> parseRedditJson(const std::string &body, std::size_t limit)
{
    const Json::Value root = parseJson(body);
    std::vector<Item> output;
    for (const auto &child : root["data"]["children"])
    {
        if (output.size() >= limit) break;
        const Json::Value &data = child["data"];
        const std::string id = data.get("name", "").asString().empty()
            ? data.get("id", "").asString() : data.get("name", "").asString();
        const std::string permalink = data.get("permalink", "").asString();
        if (id.empty() || permalink.empty()) continue;
        Item item;
        item.canonicalKey = "reddit:" + id;
        item.postUrl = "https://www.reddit.com" + permalink;
        item.text = data.get("title", "").asString();
        if (!data.get("selftext", "").asString().empty()) item.text += "\n\n" + data["selftext"].asString();
        if (data["created_utc"].isNumeric())
        {
            const auto seconds = std::chrono::seconds(data["created_utc"].asInt64());
            const auto time = std::chrono::system_clock::time_point(seconds);
            item.publishedAt = isoTimestamp(std::chrono::system_clock::to_time_t(time));
        }
        const std::string thumbnail = data.get("thumbnail", "").asString();
        if (thumbnail.starts_with("https://")) item.images.append(thumbnail);
        output.push_back(std::move(item));
    }
    return output;
}

std::vector<Item> parseRedditAtom(const std::string &body, std::size_t limit)
{
    std::vector<Item> output;
    std::size_t cursor = 0;
    while (output.size() < limit)
    {
        const auto begin = body.find("<entry", cursor);
        if (begin == std::string::npos) break;
        const auto end = body.find("</entry>", begin);
        if (end == std::string::npos) break;
        const std::string block = body.substr(begin, end - begin);

        std::string id = xml(block, "id");
        std::string link = argus::rss::canonicalize(argus::rss::atomLink(block));
        if (const auto query = link.find('?'); query != std::string::npos) link.erase(query);
        if (!link.starts_with("https://") ||
            !matchesHost(hostOf(link), {"www.reddit.com", "reddit.com"}))
        {
            cursor = end + 8;
            continue;
        }
        if (!capture(id, std::regex(R"(^t3_[A-Za-z0-9]+$)", std::regex::icase)))
        {
            const auto linkId = capture(
                link, std::regex(R"(/comments/([A-Za-z0-9]+)/)", std::regex::icase));
            if (!linkId)
            {
                cursor = end + 8;
                continue;
            }
            id = "t3_" + *linkId;
        }

        Item item;
        item.canonicalKey = "reddit:" + lower(id);
        item.postUrl = std::move(link);
        const std::string title = decodeHtml(xml(block, "title"));
        const std::string content = decodeHtml(xml(block, "content"));
        item.text = title;
        if (!content.empty() && content != title)
            item.text += (item.text.empty() ? "" : "\n\n") + content;
        item.updatedAt = xml(block, "updated");
        item.publishedAt = xml(block, "published");
        if (item.publishedAt.empty()) item.publishedAt = item.updatedAt;
        output.push_back(std::move(item));
        cursor = end + 8;
    }
    return output;
}

std::vector<Item> parseVkJson(const std::string &body, std::size_t limit)
{
    const Json::Value root = parseJson(body);
    std::vector<Item> output;
    for (const auto &data : root["response"]["items"])
    {
        if (output.size() >= limit) break;
        if (!data["id"].isNumeric() || !data["owner_id"].isNumeric()) continue;
        const std::string owner = std::to_string(data["owner_id"].asInt64());
        const std::string post = std::to_string(data["id"].asInt64());
        Item item;
        item.canonicalKey = "vk:" + owner + ":" + post;
        item.postUrl = "https://vk.com/wall" + owner + "_" + post;
        item.text = data.get("text", "").asString();
        if (data["date"].isNumeric()) item.publishedAt = isoTimestamp(data["date"].asInt64());
        for (const auto &attachment : data["attachments"])
        {
            if (attachment.get("type", "").asString() != "photo") continue;
            const auto &sizes = attachment["photo"]["sizes"];
            if (sizes.isArray() && !sizes.empty()) item.images.append(sizes[sizes.size() - 1].get("url", "").asString());
        }
        output.push_back(std::move(item));
    }
    return output;
}

std::vector<Item> parseGdeltJson(const std::string &body, std::size_t limit)
{
    const Json::Value root = parseJson(body);
    std::vector<Item> output;
    for (const auto &article : root["articles"])
    {
        if (output.size() >= limit) break;
        const std::string url = article.get("url", "").asString();
        if (!url.starts_with("http://") && !url.starts_with("https://")) continue;
        Item item;
        item.canonicalKey = "gdelt:" + argus::rss::canonicalize(url);
        item.postUrl = url;
        item.text = article.get("title", "").asString();
        item.publishedAt = article.get("seendate", "").asString();
        const std::string image = article.get("socialimage", "").asString();
        if (image.starts_with("https://")) item.images.append(image);
        output.push_back(std::move(item));
    }
    return output;
}

std::vector<Item> parseDsnsHtml(const std::string &html, const std::string &baseUrl,
                                std::size_t limit)
{
    const std::vector<std::string> hosts{"dsns.gov.ua", ".dsns.gov.ua"};
    const std::regex linkPattern(
        R"re(<a[^>]+href=["']([^"']*(?:/news/|/novini/)[^"']*)["'][^>]*>([\s\S]*?)</a>)re",
        std::regex::icase);
    std::vector<Item> output;
    std::set<std::string> seen;
    for (std::sregex_iterator it(html.begin(), html.end(), linkPattern), end;
         it != end && output.size() < limit; ++it)
    {
        const std::string resolved = argus::rss::resolveUrl(baseUrl, argus::rss::unescape((*it)[1].str()));
        std::string canonical = argus::rss::canonicalize(resolved);
        if (const auto query = canonical.find('?'); query != std::string::npos)
            canonical.erase(query);
        if (!matchesHost(hostOf(canonical), hosts) || !seen.insert(canonical).second) continue;
        const std::string text = decodeHtml((*it)[2].str());
        if (text.empty()) continue;
        Item item;
        item.canonicalKey = "dsns-http:" + canonical;
        item.postUrl = canonical;
        item.text = text;

        const std::size_t matchAt = static_cast<std::size_t>((*it).position());
        const std::size_t contextBegin = matchAt > 1000 ? matchAt - 1000 : 0;
        const std::size_t contextSize = std::min<std::size_t>(html.size() - contextBegin, 3000);
        const std::string context = html.substr(contextBegin, contextSize);
        if (const auto timestamp = capture(
                context, std::regex(R"re(<time[^>]*datetime=["']([^"']+)["'])re", std::regex::icase)))
            item.publishedAt = *timestamp;
        if (const auto image = capture(
                context, std::regex(R"re(<img[^>]+(?:src|data-src)=["']([^"']+)["'])re", std::regex::icase)))
        {
            const std::string imageUrl = argus::rss::resolveUrl(baseUrl, argus::rss::unescape(*image));
            if (matchesHost(hostOf(imageUrl), hosts)) item.images.append(imageUrl);
        }
        output.push_back(std::move(item));
    }
    return output;
}

std::string buildGdeltUrl(const std::string &sourceUrl, long long previousEndEpoch,
                          long long currentEndEpoch, std::size_t limit)
{
    if (currentEndEpoch <= 0) throw std::runtime_error("invalid_cursor");
    constexpr long long overlapSeconds = 15 * 60;
    constexpr long long initialWindowSeconds = 30 * 60;
    long long startEpoch = previousEndEpoch > 0
        ? previousEndEpoch - overlapSeconds
        : currentEndEpoch - initialWindowSeconds;
    if (startEpoch >= currentEndEpoch) startEpoch = currentEndEpoch - initialWindowSeconds;

    const auto queryAt = sourceUrl.find('?');
    const std::string base = sourceUrl.substr(0, queryAt);
    if (base != "https://api.gdeltproject.org/api/v2/doc/doc")
        throw std::runtime_error("invalid_source_url");

    std::vector<std::string> retained;
    if (queryAt != std::string::npos)
    {
        std::stringstream input(sourceUrl.substr(queryAt + 1));
        for (std::string part; std::getline(input, part, '&');)
            if (!part.empty() && !isGdeltControlParameter(part)) retained.push_back(part);
    }
    const bool hasQuery = std::any_of(retained.begin(), retained.end(), [](const std::string &part) {
        return lower(part.substr(0, part.find('='))) == "query";
    });
    if (!hasQuery) throw std::runtime_error("invalid_source_url");

    std::ostringstream url;
    url << base << '?';
    for (const auto &part : retained) url << part << '&';
    url << "mode=artlist&format=json&sort=datedesc&maxrecords="
        << std::min<std::size_t>(std::max<std::size_t>(limit, 1), 250)
        << "&startdatetime=" << gdeltTimestamp(static_cast<std::time_t>(startEpoch))
        << "&enddatetime=" << gdeltTimestamp(static_cast<std::time_t>(currentEndEpoch));
    return url.str();
}

Result collect(const std::string &platform, const std::string &sourceUrl,
               std::size_t limit, const HttpOptions &options, const std::string &cursor)
{
    try
    {
        if (platform == "youtube")
        {
            const std::string channel = channelFromYouTube(sourceUrl, options);
            if (channel.empty()) return {{}, "youtube_channel_unresolved"};
            const auto response = get("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel,
                                      {"youtube.com", "www.youtube.com"}, options);
            auto items = parseYouTubeFeed(response.body, limit);
            const std::string error = items.empty() ? "empty_feed" : "";
            return {std::move(items), error};
        }
        if (platform == "telegram")
        {
            const std::string channel = telegramChannel(sourceUrl);
            if (channel.empty()) return {{}, "invalid_source_url"};
            const auto response = get("https://telegram.me/s/" + channel,
                                      {"telegram.me", "t.me"}, options);
            if (challengePage(response.body)) return {{}, "challenge_required"};
            auto items = parseTelegramHtml(response.body, channel, limit);
            const std::string error = items.empty() ? "empty_feed" : "";
            return {std::move(items), error};
        }
        if (platform == "reddit")
        {
            const std::string community = redditCommunity(sourceUrl);
            if (community.empty()) return {{}, "invalid_source_url"};
            const std::string clientId = argus::pipeline::envString("REDDIT_CLIENT_ID");
            const std::string clientSecret = argus::pipeline::envString("REDDIT_CLIENT_SECRET");
            const std::string cookie = argus::pipeline::envString("REDDIT_COOKIE");
            Download response;
            bool fetched = false;
            if (!clientId.empty() && !clientSecret.empty())
            {
                try
                {
                    const std::string token = redditAccessToken(clientId, clientSecret, options);
                    const std::string url = "https://oauth.reddit.com/r/" + community +
                        "/new?raw_json=1&limit=" + std::to_string(limit);
                    response = get(url, {"oauth.reddit.com"}, options,
                                   {"Accept: application/json", "Authorization: Bearer " + token},
                                   "web:argus-dashboard:1.0 (by /u/argus_dashboard_bot)");
                    fetched = true;
                }
                catch (const std::runtime_error &error)
                {
                    if (std::string(error.what()) == "rate_limited") throw;
                }
            }
            if (!fetched && !cookie.empty())
            {
                try
                {
                    const std::string url = "https://www.reddit.com/r/" + community +
                        "/new.json?raw_json=1&limit=" + std::to_string(limit);
                    response = get(url, {"www.reddit.com"}, options,
                                   {"Accept: application/json", "Cookie: " + cookie},
                                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36");
                    fetched = true;
                }
                catch (const std::runtime_error &error)
                {
                    if (std::string(error.what()) == "rate_limited") throw;
                }
            }
            if (fetched)
            {
                auto items = parseRedditJson(response.body, limit);
                const std::string error = items.empty() ? "empty_feed" : "";
                return {std::move(items), error};
            }

            // Reddit exposes a public Atom feed for each community. Keep
            // OAuth/cookie as the preferred contracts above, but use the
            // bounded feed when credentials are absent or rejected.
            const std::string url = "https://www.reddit.com/r/" + community +
                "/new/.rss?limit=" + std::to_string(
                    std::min<std::size_t>(std::max<std::size_t>(limit, 1), 100));
            response = get(url, {"www.reddit.com"}, options,
                           {"Accept: application/atom+xml, application/xml;q=0.9"},
                           "ArgusPRO/1.0 (+https://argusnews.online)");
            auto items = parseRedditAtom(response.body, limit);
            const std::string error = items.empty() ? "empty_feed" : "";
            return {std::move(items), error};
        }
        if (platform == "vk")
        {
            const std::string token = argus::pipeline::envString("VK_TOKEN");
            if (token.empty()) return {{}, "credentials_missing:VK_TOKEN"};
            const std::string slug = vkSlug(sourceUrl);
            if (slug.empty()) return {{}, "invalid_source_url"};
            const auto resolved = get("https://api.vk.com/method/utils.resolveScreenName?v=5.199&screen_name=" + urlEncode(slug) + "&access_token=" + urlEncode(token),
                                      {"api.vk.com"}, options);
            const Json::Value identity = parseJson(resolved.body)["response"];
            if (!identity.isObject() || !identity["object_id"].isNumeric()) return {{}, "vk_source_unresolved"};
            long long owner = identity["object_id"].asInt64();
            const std::string type = identity.get("type", "").asString();
            if (type == "group" || type == "page") owner = -owner;
            const auto response = get("https://api.vk.com/method/wall.get?v=5.199&filter=owner&count=" + std::to_string(limit) + "&owner_id=" + std::to_string(owner) + "&access_token=" + urlEncode(token),
                                      {"api.vk.com"}, options);
            auto items = parseVkJson(response.body, limit);
            const std::string error = items.empty() ? "empty_feed" : "";
            return {std::move(items), error};
        }
        if (platform == "gdelt")
        {
            if (!matchesHost(hostOf(sourceUrl), {"api.gdeltproject.org"})) return {{}, "invalid_source_url"};
            long long previousEnd = 0;
            if (!cursor.empty())
            {
                try { previousEnd = std::stoll(cursor); }
                catch (...) { return {{}, "invalid_cursor"}; }
            }
            const long long currentEnd = static_cast<long long>(std::time(nullptr));
            const std::string url = buildGdeltUrl(sourceUrl, previousEnd, currentEnd, limit);
            const auto response = get(url, {"api.gdeltproject.org"}, options);
            auto items = parseGdeltJson(response.body, limit);
            // An empty bounded DOC window is a valid observation. Advancing
            // the cursor prevents an empty query from retrying the same range
            // forever; the next run still overlaps 15 minutes for late data.
            return {std::move(items), {}, std::to_string(currentEnd)};
        }
        if (platform == "dsns-http") return collectDsns(sourceUrl, limit, options);
        if (platform == "max") return {{}, "unsupported_contract"};
        return {{}, "unsupported_platform"};
    }
    catch (const std::runtime_error &error)
    {
        return {{}, error.what()};
    }
    catch (...)
    {
        return {{}, "collection_failed"};
    }
}

}  // namespace argus::collectors
