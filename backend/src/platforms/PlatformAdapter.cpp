#include "PlatformAdapter.h"

#include <curl/curl.h>

#include <algorithm>
#include <cctype>
#include <regex>
#include <set>
#include <utility>

namespace argus::browser {
namespace {

std::string lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

std::string nestedText(const Json::Value &value)
{
    for (const char *key : {"text", "caption", "title", "description", "desc", "full_text", "message", "body"})
    {
        if (value[key].isString() && !value[key].asString().empty()) return value[key].asString();
        if (value[key].isObject())
        {
            const std::string nested = nestedText(value[key]);
            if (!nested.empty()) return nested;
        }
    }
    return {};
}

std::string firstString(const Json::Value &value,
                        const std::initializer_list<const char *> &keys)
{
    for (const char *key : keys)
        if (value[key].isString() && !value[key].asString().empty()) return value[key].asString();
    return {};
}

std::string firstNestedString(const Json::Value &value,
                              const std::initializer_list<const char *> &objects,
                              const std::initializer_list<const char *> &keys)
{
    for (const char *object : objects)
    {
        const Json::Value &nested = value[object];
        if (!nested.isObject()) continue;
        if (const std::string result = firstString(nested, keys); !result.empty()) return result;
    }
    return {};
}

std::string networkTimestamp(const Json::Value &value, const AdapterSpec &spec)
{
    for (const auto &field : spec.networkTimestampFields)
    {
        const Json::Value &raw = value[field];
        if (raw.isString()) return raw.asString();
        if (raw.isNumeric()) return std::to_string(raw.asInt64());
    }
    return {};
}

std::string networkIdentity(const Json::Value &value, const AdapterSpec &spec)
{
    for (const auto &field : spec.networkIdentityFields)
    {
        const Json::Value &raw = value[field];
        if (raw.isString() && !raw.asString().empty()) return raw.asString();
        if (raw.isNumeric()) return std::to_string(raw.asInt64());
    }
    return {};
}

std::string fillTemplate(std::string value, const std::string &identity)
{
    const std::string marker = "{id}";
    if (const auto at = value.find(marker); at != std::string::npos)
        value.replace(at, marker.size(), identity);
    return value;
}

void walkNetwork(const Json::Value &value, const AdapterSpec &spec,
                 const std::string &sourceUrl, const std::string &expectedTikTokHandle,
                 std::vector<Json::Value> &rows,
                 int depth = 0)
{
    if (depth > 25) return;
    if (value.isArray())
    {
        for (const auto &child : value)
            walkNetwork(child, spec, sourceUrl, expectedTikTokHandle, rows, depth + 1);
        return;
    }
    if (!value.isObject()) return;
    const std::string typeName = firstString(value, {"__typename", "typename"});
    if (spec.platform == "facebook")
    {
        const std::string normalizedType = lower(typeName);
        if (normalizedType == "comment" || normalizedType == "xfbcommentdeprecated" ||
            value.isMember("comment_id") || value.isMember("reply_comment_id")) return;
    }
    const std::string identity = networkIdentity(value, spec);
    const std::string timestamp = networkTimestamp(value, spec);
    std::string url = firstString(value, {"permalink_url", "story_url", "post_url", "url", "link"});
    std::string ownerUrl;
    std::string ownerId;
    std::string ownerHandle;
    if (spec.platform == "facebook")
    {
        if (url.empty())
        {
            url = firstNestedString(value, {"feedback"}, {"url"});
            if (url.empty()) url = firstNestedString(value, {"share"}, {"uri", "url"});
        }
        ownerUrl = firstString(value, {"owner_url", "profile_url", "actor_url"});
        if (ownerUrl.empty())
            ownerUrl = firstNestedString(value, {"actor", "author", "owner"},
                                         {"url", "profile_url"});
        if (ownerUrl.empty() && value["actors"].isArray() && !value["actors"].empty())
            ownerUrl = firstString(value["actors"][0], {"url", "profile_url"});
        ownerId = firstString(value, {"owner_id", "page_id", "profile_id", "actor_id",
                                      "content_owner_id_new"});
        if (ownerId.empty())
            ownerId = firstNestedString(value, {"actor", "author", "owner"},
                                        {"id", "profile_id"});
    }
    else if (spec.platform == "threads")
    {
        ownerHandle = firstString(value, {"username", "user_name", "handle"});
        if (ownerHandle.empty())
            ownerHandle = firstNestedString(value, {"user", "author", "owner", "profile"},
                                            {"username", "user_name", "handle"});
        // Threads GraphQL payloads often expose only the post code plus the
        // nested author's username. Build the permalink from both so the
        // source verifier can reject suggested posts from other profiles.
        if (!ownerHandle.empty() && !identity.empty())
            url = "https://www.threads.com/@" + ownerHandle + "/post/" + identity;
    }
    bool validSourceIdentity = true;
    if (spec.platform == "tiktok")
    {
        const Json::Value &author = value["author"];
        const std::string authorHandle = author.isString()
            ? lower(author.asString())
            : (author.isObject()
                ? lower(firstString(author, {"uniqueId", "unique_id", "uniqueName", "username"}))
                : std::string{});
        validSourceIdentity = !expectedTikTokHandle.empty() &&
                              authorHandle == expectedTikTokHandle;
        if (!url.empty())
        {
            const std::string postHandle = lower(regexCapture(
                httpUrlPath(url), R"(^/@([A-Za-z0-9._-]+)/video/\d{8,}/?$)"));
            validSourceIdentity = validSourceIdentity && postHandle == expectedTikTokHandle;
        }
        else if (validSourceIdentity && !identity.empty())
            url = "https://www.tiktok.com/@" + expectedTikTokHandle + "/video/" + identity;
    }
    else if (url.empty() && !identity.empty() && !spec.networkUrlTemplate.empty())
        url = fillTemplate(spec.networkUrlTemplate, identity);
    if (validSourceIdentity && (!identity.empty() || !url.empty()) && !timestamp.empty())
    {
        Json::Value row;
        row["url"] = url;
        row["identity"] = identity;
        row["text"] = nestedText(value);
        row["publishedAt"] = timestamp;
        if (!typeName.empty()) row["typename"] = typeName;
        if (!ownerUrl.empty()) row["ownerUrl"] = ownerUrl;
        if (!ownerId.empty()) row["ownerId"] = ownerId;
        if (!ownerHandle.empty()) row["ownerHandle"] = ownerHandle;
        const std::string image = firstString(value, {"display_url", "thumbnail_src", "image_url"});
        if (!image.empty()) row["images"].append(image);
        if (!spec.sourceVerifier || spec.sourceVerifier(row, sourceUrl))
            rows.push_back(std::move(row));
    }
    for (const auto &name : value.getMemberNames())
        walkNetwork(value[name], spec, sourceUrl, expectedTikTokHandle, rows, depth + 1);
}

class DomAdapter final : public PlatformAdapter {
  public:
    explicit DomAdapter(AdapterSpec spec) : spec_(std::move(spec)) {}
    const AdapterSpec &spec() const override { return spec_; }

    std::string navigationUrl(const std::string &sourceUrl) const override
    {
        if (spec_.navigationTransform) return spec_.navigationTransform(sourceUrl);
        return PlatformAdapter::navigationUrl(sourceUrl);
    }

    std::vector<BrowserItem> parseDom(const Json::Value &raw, std::size_t limit) const override
    {
        std::vector<BrowserItem> output;
        std::set<std::string> seen;
        if (!raw.isArray()) return output;
        for (const auto &row : raw)
        {
            if (output.size() >= limit) break;
            const auto [key, canonicalUrl] = spec_.canonicalizer(row.get("url", "").asString());
            if (key.empty() || canonicalUrl.empty() || !seen.insert(key).second) continue;
            BrowserItem item;
            item.canonicalKey = key;
            item.postUrl = canonicalUrl;
            item.text = row.get("text", "").asString();
            item.publishedAt = row.get("publishedAt", "").asString();
            if (row["images"].isArray())
                for (const auto &image : row["images"])
                    if (image.isString() && image.asString().rfind("http", 0) == 0)
                        item.images.append(image.asString());
            if (row["videos"].isArray())
                for (const auto &video : row["videos"])
                    if (video.isString() && video.asString().rfind("http", 0) == 0)
                        item.videos.append(video.asString());
            output.push_back(std::move(item));
        }
        return output;
    }

    std::vector<BrowserItem> parseNetwork(const std::vector<Json::Value> &responses,
                                          const std::string &sourceUrl,
                                          std::size_t limit) const override
    {
        std::vector<Json::Value> raw;
        std::string expectedTikTokHandle;
        if (spec_.platform == "tiktok")
            expectedTikTokHandle = lower(regexCapture(
                httpUrlPath(sourceUrl), R"(^/@([A-Za-z0-9._-]+)/?$)"));
        for (const auto &response : responses)
            walkNetwork(response, spec_, sourceUrl, expectedTikTokHandle, raw);
        for (auto &row : raw)
        {
            if (!row.get("url", "").asString().empty()) continue;
            const std::string identity = row.get("identity", "").asString();
            if (!identity.empty()) row["url"] = fillTemplate(spec_.networkUrlTemplate, identity);
        }
        Json::Value rows(Json::arrayValue);
        for (auto &row : raw) rows.append(std::move(row));
        return parseDom(rows, limit);
    }

  private:
    AdapterSpec spec_;
};

}  // namespace

std::string PlatformAdapter::navigationUrl(const std::string &sourceUrl) const { return sourceUrl; }

std::string PlatformAdapter::fallbackNavigationUrl(const std::string &sourceUrl) const
{
    if (spec().fallbackNavigationTransform) return spec().fallbackNavigationTransform(sourceUrl);
    return {};
}

bool PlatformAdapter::needsFallback(std::size_t verifiedItems) const
{
    return spec().fallbackThreshold > 0 && verifiedItems < spec().fallbackThreshold &&
           spec().fallbackNavigationTransform && !spec().fallbackExtractionExpression.empty();
}

std::vector<BrowserItem> PlatformAdapter::parseDomForSource(const Json::Value &raw,
                                                            const std::string &sourceUrl,
                                                            std::size_t limit) const
{
    if (!spec().sourceVerifier) return parseDom(raw, limit);
    if (!raw.isArray()) return {};
    Json::Value verified(Json::arrayValue);
    for (const auto &row : raw)
        if (spec().sourceVerifier(row, sourceUrl)) verified.append(row);
    return parseDom(verified, limit);
}

std::vector<BrowserItem> PlatformAdapter::parseFallbackDom(const Json::Value &raw,
                                                           const std::string &sourceUrl,
                                                           std::size_t limit) const
{
    return parseDomForSource(raw, sourceUrl, limit);
}

std::string PlatformAdapter::classifyPage(const std::string &currentUrl,
                                          const std::string &bodyText) const
{
    const std::string url = lower(currentUrl);
    const std::string sample = lower(currentUrl + "\n" + bodyText.substr(0, 100000));
    if ((spec().platform == "tiktok" &&
         (sample.find("slardarwaf") != std::string::npos ||
          sample.find("slardar_us_waf") != std::string::npos ||
          sample.find("_wafchallengeid") != std::string::npos ||
          sample.find("waforiginalreid") != std::string::npos)) ||
        sample.find("cf-turnstile") != std::string::npos ||
        sample.find("challenge-platform") != std::string::npos ||
        sample.find("just a moment") != std::string::npos ||
        sample.find("captcha") != std::string::npos ||
        sample.find("verify you are human") != std::string::npos) return "challenge_required";
    if (sample.find("too many requests") != std::string::npos ||
        sample.find("rate limit") != std::string::npos || sample.find("error 429") != std::string::npos)
        return "rate_limited";
    // Public feeds commonly include a generic "Log in" call-to-action even
    // when their posts are already present in the DOM. Treat only an actual
    // navigation to an authentication/checkpoint route as an auth failure;
    // selector/extraction checks below decide whether public content exists.
    if (url.find("/login") != std::string::npos || url.find("/checkpoint") != std::string::npos ||
        url.find("/accounts/login") != std::string::npos || url.find("/i/flow/login") != std::string::npos)
        return "auth_required";
    return {};
}

std::string PlatformAdapter::classifyEmptyPage(const std::string &currentUrl,
                                               const std::string &bodyText) const
{
    if (const std::string classified = classifyPage(currentUrl, bodyText); !classified.empty())
        return classified;
    if (std::all_of(bodyText.begin(), bodyText.end(),
                    [](unsigned char character) { return std::isspace(character); }))
        return "empty_shell";
    const std::string sample = lower(bodyText.substr(0, 100000));
    if (spec().platform == "tiktok" && sample.size() < 256 &&
        sample.find("please wait") != std::string::npos)
        return "challenge_required";
    if (spec().platform != "threads") return {};

    // A generic login CTA is harmless when public posts are present, so the
    // normal page classifier intentionally ignores it. Once both DOM and
    // network extraction are empty, the same CTA identifies a soft login wall
    // without logging page text, URLs, cookies, or any private content.
    if (sample.find("log in") != std::string::npos ||
        sample.find("sign in") != std::string::npos ||
        bodyText.find("Увійти") != std::string::npos ||
        bodyText.find("увійти") != std::string::npos ||
        bodyText.find("Войти") != std::string::npos ||
        bodyText.find("войти") != std::string::npos)
        return "auth_required";
    return {};
}

std::unique_ptr<PlatformAdapter> makeDomAdapter(AdapterSpec spec)
{
    return std::make_unique<DomAdapter>(std::move(spec));
}

std::unique_ptr<PlatformAdapter> makePlatformAdapter(const std::string &platform)
{
    if (platform == "facebook") return makeFacebookAdapter();
    if (platform == "instagram") return makeInstagramAdapter();
    if (platform == "x") return makeXAdapter();
    if (platform == "threads") return makeThreadsAdapter();
    if (platform == "tiktok") return makeTikTokAdapter();
    if (platform == "vk") return makeVkAdapter();
    if (platform == "ok") return makeOkAdapter();
    if (platform == "max") return makeMaxAdapter();
    if (platform == "dsns-browser") return makeDsnsBrowserAdapter();
    return nullptr;
}

bool allowedHttpUrl(const std::string &url, const std::vector<std::string> &hosts)
{
    CURLU *parsed = curl_url();
    if (!parsed) return false;
    if (curl_url_set(parsed, CURLUPART_URL, url.c_str(), 0) != CURLUE_OK)
    {
        curl_url_cleanup(parsed);
        return false;
    }
    auto get = [&](CURLUPart part) {
        char *raw = nullptr;
        std::string value;
        if (curl_url_get(parsed, part, &raw, 0) == CURLUE_OK && raw)
        {
            value = lower(raw);
            curl_free(raw);
        }
        return value;
    };
    const std::string scheme = get(CURLUPART_SCHEME);
    const std::string host = get(CURLUPART_HOST);
    curl_url_cleanup(parsed);
    if (scheme != "http" && scheme != "https") return false;
    for (const auto &candidate : hosts)
    {
        const std::string allowed = lower(candidate);
        if (host == allowed) return true;
        if (!allowed.empty() && allowed.front() == '.' && host.size() > allowed.size() && host.ends_with(allowed))
            return true;
    }
    return false;
}

std::string httpUrlPath(const std::string &url)
{
    CURLU *parsed = curl_url();
    if (!parsed) return {};
    if (curl_url_set(parsed, CURLUPART_URL, url.c_str(), 0) != CURLUE_OK)
    {
        curl_url_cleanup(parsed);
        return {};
    }
    char *raw = nullptr;
    std::string path;
    if (curl_url_get(parsed, CURLUPART_PATH, &raw, 0) == CURLUE_OK && raw)
    {
        path = raw;
        curl_free(raw);
    }
    curl_url_cleanup(parsed);
    return path;
}

std::string stripFragment(const std::string &url)
{
    return url.substr(0, url.find('#'));
}

std::string stripQueryAndFragment(const std::string &url)
{
    return url.substr(0, std::min(url.find('?'), url.find('#')));
}

std::string regexCapture(const std::string &value, const std::string &pattern, std::size_t group)
{
    std::smatch match;
    if (std::regex_search(value, match, std::regex(pattern, std::regex::icase)) && group < match.size())
        return match[group].str();
    return {};
}

}  // namespace argus::browser
