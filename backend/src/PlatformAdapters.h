#pragma once

#include <json/json.h>

#include <cstddef>
#include <string>
#include <vector>

namespace argus::collectors {

struct Item {
    std::string canonicalKey;
    std::string postUrl;
    std::string text;
    std::string publishedAt;
    std::string updatedAt;
    Json::Value images{Json::arrayValue};
    Json::Value videos{Json::arrayValue};
};

struct Result {
    std::vector<Item> items;
    std::string errorCode;
    // Opaque collector cursor. It is persisted only after a successful
    // shadow collection, so a failed run replays the same bounded window.
    std::string nextCursor;
};

struct HttpOptions {
    long connectTimeoutSeconds{5};
    long totalTimeoutSeconds{15};
    std::size_t maxBytes{5U * 1024U * 1024U};
};

std::vector<Item> parseYouTubeFeed(const std::string &xml, std::size_t limit);
// Resolves an ID only for an allowlisted YouTube channel URL. Direct
// /channel/ and channel_id query URLs do not require HTML; standard handle,
// /c/, /user/, and legacy single-segment vanity pages require channel-owned
// metadata in `html`. Video URLs and foreign hosts are never promoted to a
// channel source.
std::string youtubeChannelIdFromSource(const std::string &sourceUrl,
                                       const std::string &html = {});
std::vector<Item> parseTelegramHtml(const std::string &html, const std::string &channel,
                                    std::size_t limit);
std::vector<Item> parseRedditJson(const std::string &json, std::size_t limit);
std::vector<Item> parseRedditAtom(const std::string &xml, std::size_t limit);
std::vector<Item> parseVkJson(const std::string &json, std::size_t limit);
std::vector<Item> parseGdeltJson(const std::string &json, std::size_t limit);
std::vector<Item> parseDsnsHtml(const std::string &html, const std::string &baseUrl,
                                std::size_t limit);
bool challengePage(const std::string &body);

// Deterministic helper used by the GDELT collector and its contract tests.
// Cursor values are Unix seconds; a 15-minute overlap admits late documents
// while canonical keys make replay idempotent.
std::string buildGdeltUrl(const std::string &sourceUrl, long long previousEndEpoch,
                          long long currentEndEpoch, std::size_t limit);

// Performs one bounded collection call. Every adapter validates both the source
// hostname and the final redirect hostname before parsing the response.
Result collect(const std::string &platform, const std::string &sourceUrl,
               std::size_t limit, const HttpOptions &options = {},
               const std::string &cursor = {});

}  // namespace argus::collectors
