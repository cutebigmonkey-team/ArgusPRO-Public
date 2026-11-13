#include "Database.h"

#include <drogon/drogon.h>
#include <drogon/orm/DbClient.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <charconv>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <memory>
#include <sstream>
#include <string>

using namespace drogon;

namespace argus::backend::database {
namespace {

std::shared_ptr<orm::DbClient> g_db;
std::string g_configError = "database_not_configured";

std::string env(const char *name)
{
    const char *value = std::getenv(name);
    if (value != nullptr && *value != '\0') return std::string(value);
    std::ifstream secrets("/run/secrets/arguspro.env");
    std::string line;
    const std::string prefix = std::string(name) + "=";
    while (std::getline(secrets, line))
    {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.rfind(prefix, 0) == 0) return line.substr(prefix.size());
    }
    return {};
}

int boundedInt(const char *name, int fallback, int minimum, int maximum)
{
    const std::string value = env(name);
    int parsed = fallback;
    const auto result = std::from_chars(value.data(), value.data() + value.size(), parsed);
    if (value.empty() || result.ec != std::errc{} || result.ptr != value.data() + value.size()) return fallback;
    return std::clamp(parsed, minimum, maximum);
}

std::string addParameter(std::string connection, const std::string &key, const std::string &value)
{
    if (value.empty()) return connection;
    if (connection.find("postgresql://") == 0 || connection.find("postgres://") == 0)
        return connection + (connection.find('?') == std::string::npos ? "?" : "&") + key + "=" + value;
    return connection + " " + key + "='" + value + "'";
}

// Every handler in this file serves data that changes on each poll of the
// collector pipeline (posts, platforms, analytics). Without an explicit
// no-store, a response lacking any cache-control/etag/last-modified header
// is fair game for an intermediary (ISP/corporate proxy, some browsers) to
// cache heuristically, which would make new posts invisible behind a stale
// cached response even after a client-side hard refresh.
HttpResponsePtr jsonResponse(const Json::Value &json)
{
    auto result = HttpResponse::newHttpJsonResponse(json);
    result->addHeader("Cache-Control", "no-store");
    return result;
}

HttpResponsePtr response(const char *status, const char *reason, HttpStatusCode code)
{
    Json::Value json;
    json["status"] = status;
    if (reason != nullptr) json["reason"] = reason;
    auto result = jsonResponse(json);
    result->setStatusCode(code);
    return result;
}

void queryWithRetry(const std::shared_ptr<orm::DbClient> &client,
                    std::function<void(const HttpResponsePtr &)> callback,
                    unsigned int attempt)
{
    client->execSqlAsync(
        "SELECT 1",
        [callback](const orm::Result &) { callback(response("ok", nullptr, k200OK)); },
        [client, callback, attempt](const orm::DrogonDbException &) {
            constexpr unsigned int kMaximumAttempts = 3;
            if (attempt + 1 < kMaximumAttempts)
            {
                const double delaySeconds = 0.25 * static_cast<double>(1U << attempt);
                app().getLoop()->runAfter(delaySeconds, [client, callback, attempt] {
                    queryWithRetry(client, callback, attempt + 1);
                });
                return;
            }
            // Intentionally omit exception text: libpq errors can contain connection details.
            callback(response("unavailable", "database_query_failed", k503ServiceUnavailable));
        });
}

void checkDatabase(std::function<void(const HttpResponsePtr &)> callback)
{
    if (!g_db)
    {
        callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable));
        return;
    }
    queryWithRetry(g_db, std::move(callback), 0);
}

// Keywords/групи taxonomy — colors and categories are cosmetic UI metadata,
// not stored in Postgres (keyword_groups/keywords have no such columns), so
// they're assigned/labeled here instead of adding schema for pure display.
constexpr std::array<const char *, 10> kGroupColors = {
    "rose", "amber", "emerald", "orange", "blue", "indigo", "slate", "cyan", "purple", "teal",
};
struct KeywordCategoryMeta { const char *id; const char *label; };
constexpr std::array<KeywordCategoryMeta, 7> kKeywordCategories = {{
    {"shelling", "Обстріли"}, {"fire", "Пожежі"}, {"accident", "Аварії"},
    {"industrial", "Техногенні"}, {"natural", "Природні"},
    {"ru_platforms", "RU vk-ok-max"}, {"info", "Інформаційні"},
}};

// Escapes ILIKE wildcard metacharacters in a keyword value so matching is a
// literal substring search, not an accidental pattern (a keyword containing
// "%" or "_" must still be matched literally).
std::string escapeLikePattern(const std::string &s)
{
    std::string out;
    out.reserve(s.size());
    for (char c : s)
    {
        if (c == '\\' || c == '%' || c == '_') out.push_back('\\');
        out.push_back(c);
    }
    return out;
}

bool isPlatformKey(const std::string &value)
{
    constexpr size_t kMaximumPlatformKeyLength = 64;
    if (value.empty() || value.size() > kMaximumPlatformKeyLength) return false;

    const auto isAsciiLowerOrDigit = [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
    };
    if (!isAsciiLowerOrDigit(static_cast<unsigned char>(value.front())) ||
        !isAsciiLowerOrDigit(static_cast<unsigned char>(value.back())))
        return false;

    return std::all_of(value.begin(), value.end(), [&](unsigned char c) {
        return isAsciiLowerOrDigit(c) || c == '-';
    });
}

std::string trimWhitespace(std::string value)
{
    const auto nonSpace = [](unsigned char c) { return !std::isspace(c); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), nonSpace));
    value.erase(std::find_if(value.rbegin(), value.rend(), nonSpace).base(), value.end());
    return value;
}

std::string vkAuthoredText(std::string value)
{
    // Older rows may contain the localized VK controls/comments captured
    // after the authored body. Strip only at a line boundary for display;
    // keep the raw database row untouched.
    std::size_t marker = std::string::npos;
    for (const std::string label : {"Оцінили", "Оценили", "Liked by"})
    {
        std::size_t at = value.find(label);
        while (at != std::string::npos && at > 0 && value[at - 1] != '\n')
            at = value.find(label, at + label.size());
        if (at != std::string::npos) marker = std::min(marker, at);
    }
    if (marker != std::string::npos)
    {
        std::size_t serviceStart = marker > 0 ? marker - 1 : 0;
        if (serviceStart > 0)
        {
            const std::size_t previousBreak = value.rfind('\n', serviceStart - 1);
            const std::size_t countStart = previousBreak == std::string::npos ? 0 : previousBreak + 1;
            const std::string count = value.substr(countStart, serviceStart - countStart);
            if (!count.empty() && std::all_of(count.begin(), count.end(),
                                               [](unsigned char c) { return std::isdigit(c); }))
                serviceStart = previousBreak == std::string::npos ? 0 : previousBreak;
        }
        value.erase(serviceStart);
    }

    value = trimWhitespace(std::move(value));
    for (const std::string suffix : {"Показати ще", "Показать ещё", "Show more"})
    {
        if (value.size() < suffix.size() ||
            value.compare(value.size() - suffix.size(), suffix.size(), suffix) != 0) continue;
        value.erase(value.size() - suffix.size());
        value = trimWhitespace(std::move(value));
        break;
    }
    return value;
}

}  // namespace

void configure()
{
    std::string connection = env("DATABASE_URL");
    const std::string sslmode = env("DB_SSLMODE");
    const std::string caPath = env("SUPABASE_CA_CERT_PATH");
    const int poolSize = boundedInt("DB_POOL_SIZE", 3, 1, 5);
    const int connectTimeout = boundedInt("DB_CONNECT_TIMEOUT", 10, 1, 60);

    if (connection.empty()) g_configError = "database_url_missing";
    else if (sslmode != "verify-full") g_configError = "db_sslmode_must_be_verify_full";
    else if (caPath.empty() || !std::filesystem::is_regular_file(caPath)) g_configError = "database_ca_certificate_missing";
    else
    {
        connection = addParameter(std::move(connection), "sslmode", sslmode);
        connection = addParameter(std::move(connection), "sslrootcert", caPath);
        connection = addParameter(std::move(connection), "connect_timeout", std::to_string(connectTimeout));
        try
        {
            // Drogon/libpq reconnects failed pool connections asynchronously. Startup and
            // process health therefore remain independent of a temporary database outage.
            g_db = orm::DbClient::newPgClient(connection, static_cast<size_t>(poolSize));
            g_configError.clear();
        }
        catch (const std::exception &)
        {
            g_configError = "database_client_initialization_failed";
        }
    }

    app().registerHandler("/health/db", [](const HttpRequestPtr &,
                                            std::function<void(const HttpResponsePtr &)> &&callback) {
        checkDatabase(std::move(callback));
    }, {Get});
    app().registerHandler("/ready", [](const HttpRequestPtr &,
                                        std::function<void(const HttpResponsePtr &)> &&callback) {
        checkDatabase(std::move(callback));
    }, {Get});

    app().registerHandler("/api/platforms", [](const HttpRequestPtr &,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        g_db->execSqlAsync(
            // dsns-http/dsns-browser are internal collector-routing platform
            // entries (migrations/0037_collector_platforms.sql), not
            // user-facing content sources — excluded from the Home feed's
            // platform filter.
            "SELECT key,name FROM platforms WHERE enabled AND key NOT IN ('dsns-http','dsns-browser') ORDER BY name",
            [callback](const orm::Result &rows) {
                Json::Value items(Json::arrayValue);
                Json::Value all; all["id"]="all"; all["label"]="Усі джерела"; items.append(all);
                for (const auto &row : rows) { Json::Value item; item["id"]=row["key"].as<std::string>(); item["label"]=row["name"].as<std::string>(); items.append(item); }
                Json::Value json; json["platforms"] = items; callback(jsonResponse(json));
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
    }, {Get});

    app().registerHandler("/api/posts", [](const HttpRequestPtr &req,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        const int limit = std::clamp([&] { try { return std::stoi(req->getParameter("limit")); } catch (...) { return 100; } }(), 1, 1000);
        std::string platform = req->getParameter("platform");
        if (platform == "all") platform.clear();
        if (!platform.empty() && !isPlatformKey(platform))
        {
            callback(response("error", "invalid_platform", k400BadRequest));
            return;
        }
        const std::string origin = req->getParameter("origin");
        if (!origin.empty() && origin != "general" && origin != "dsns")
        {
            callback(response("error", "invalid_origin", k400BadRequest));
            return;
        }
        const std::string topic = req->getParameter("topic");
        if (!topic.empty() && topic != "dsns")
        {
            callback(response("error", "invalid_topic", k400BadRequest));
            return;
        }
        const std::string tone = req->getParameter("tone");
        if (!tone.empty() && (topic != "dsns" ||
            (tone != "praise" && tone != "criticism" && tone != "other")))
        {
            callback(response("error", "invalid_tone", k400BadRequest));
            return;
        }
        const std::string publisher = req->getParameter("publisher");
        if (!publisher.empty() && publisher != "official_dsns" && publisher != "external")
        {
            callback(response("error", "invalid_publisher", k400BadRequest));
            return;
        }
        const std::string balance = req->getParameter("balance");
        if (!balance.empty() && (balance != "platform" || !topic.empty()))
        {
            callback(response("error", "invalid_balance", k400BadRequest));
            return;
        }
        const std::string requireTextParameter = req->getParameter("require_text");
        if (!requireTextParameter.empty() && requireTextParameter != "true")
        {
            callback(response("error", "invalid_require_text", k400BadRequest));
            return;
        }
        const bool requireText = requireTextParameter == "true";

        const std::string balancedPosts =
            "WITH sampled AS ("
            " SELECT po.id,pl.key AS platform,po.origin,po.legacy_source_id,po.source_name,"
            " COALESCE(r.name,'') AS region,COALESCE(po.published_at,po.collected_at) AS published_at,"
            " po.text,po.content,po.post_url,COALESCE(po.likes,0) AS likes,"
            " COALESCE(po.comments_count,0) AS comments_count,COALESCE(po.views,0) AS views,"
            " po.images,po.sort_ts,"
            " lower(COALESCE(po.text,'')||' '||COALESCE(po.content,'')) AS body"
            " FROM platforms pl"
            " CROSS JOIN LATERAL ("
            "  SELECT candidate.* FROM posts candidate"
            "  LEFT JOIN sources candidate_src ON candidate_src.id=candidate.source_id"
            "  WHERE candidate.platform_id=pl.id"
            "  AND ($3::text='' OR candidate.origin=$3::text)"
            "  AND ($6::text=''"
            "   OR ($6::text='official_dsns' AND COALESCE(candidate_src.is_official_dsns,false))"
            "   OR ($6::text='external' AND NOT COALESCE(candidate_src.is_official_dsns,false)))"
            "  AND (pl.key<>'threads' OR lower(substring(candidate.post_url from '/@([^/?#]+)/post/'))="
            "   lower(substring(COALESCE(candidate.source_page_url,candidate_src.url) from '/@([^/?#]+)')))"
            "  ORDER BY candidate.sort_ts DESC,candidate.id DESC"
            "  LIMIT CASE WHEN $2::text='' THEN LEAST($1::int,200) ELSE $1::int END"
            " ) po"
            " LEFT JOIN regions r ON r.id=po.region_id"
            " WHERE ($2::text='' OR pl.key=$2::text)"
            " AND $4::text='' AND $5::text='' AND $7::text='platform'"
            "), classified AS ("
            " SELECT sampled.*,CASE"
            " WHEN body !~ '(дснс|гсчс|рятуваль|надзвичайн|пожежник|спасател|emergency service)' THEN ''"
            " WHEN body ~ '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)' THEN 'criticism'"
            " WHEN body ~ '(дяку|подяк|молодц|геро|пишаєм|вдячн|спасиб|hero|thank)' THEN 'praise'"
            " ELSE 'other' END AS mention_tone"
            " FROM sampled"
            "), ranked AS ("
            " SELECT classified.*,row_number() OVER (PARTITION BY platform ORDER BY sort_ts DESC,id DESC) AS platform_rank"
            " FROM classified"
            ") SELECT id::text,platform,origin,legacy_source_id,source_name,region,published_at::text,"
            " text,content,post_url,likes,comments_count,views,images::text,mention_tone"
            " FROM ranked ORDER BY platform_rank,sort_ts DESC,id DESC"
            " LIMIT CASE WHEN $8::text='true' THEN LEAST($1::int*2,2000) ELSE $1::int END";

        const std::string selectPosts = !balance.empty() ? balancedPosts : topic.empty() ?
            // The ordinary feed stays on the existing indexed recency path;
            // computing a display-only tone label for the bounded result does
            // not introduce a whole-table window/sort.
            std::string(
            "SELECT po.id::text,pl.key AS platform,po.origin,po.legacy_source_id,po.source_name,"
            "COALESCE(r.name,'') AS region,COALESCE(po.published_at,po.collected_at)::text AS published_at,"
            "po.text,po.content,po.post_url,COALESCE(po.likes,0) AS likes,COALESCE(po.comments_count,0) AS comments_count,"
            "COALESCE(po.views,0) AS views,po.images::text,CASE"
            " WHEN lower(COALESCE(po.text,'')||' '||COALESCE(po.content,'')) !~ '(дснс|гсчс|рятуваль|надзвичайн|пожежник|спасател|emergency service)' THEN ''"
            " WHEN lower(COALESCE(po.text,'')||' '||COALESCE(po.content,'')) ~ '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)' THEN 'criticism'"
            " WHEN lower(COALESCE(po.text,'')||' '||COALESCE(po.content,'')) ~ '(дяку|подяк|молодц|геро|пишаєм|вдячн|спасиб|hero|thank)' THEN 'praise'"
            " ELSE 'other' END AS mention_tone"
            " FROM posts po JOIN platforms pl ON pl.id=po.platform_id"
            " LEFT JOIN sources src ON src.id=po.source_id LEFT JOIN regions r ON r.id=po.region_id"
            " WHERE ($2::text='' OR pl.key=$2::text) AND ($3::text='' OR po.origin=$3::text)"
            " AND ($6::text=''"
            " OR ($6::text='official_dsns' AND COALESCE(src.is_official_dsns,false))"
            " OR ($6::text='external' AND NOT COALESCE(src.is_official_dsns,false)))"
            " AND $4::text='' AND $5::text=''"
            " AND (pl.key<>'threads' OR lower(substring(po.post_url from '/@([^/?#]+)/post/'))="
            " lower(substring(COALESCE(po.source_page_url,src.url) from '/@([^/?#]+)')))"
            " ORDER BY po.sort_ts DESC,po.id DESC LIMIT $1::int") :
            std::string(
            "WITH base AS ("
            " SELECT po.id,pl.key AS platform,po.origin,po.legacy_source_id,po.source_name,"
            " COALESCE(r.name,'') AS region,COALESCE(po.published_at,po.collected_at) AS published_at,"
            " po.text,po.content,po.post_url,COALESCE(po.likes,0) AS likes,"
            " COALESCE(po.comments_count,0) AS comments_count,COALESCE(po.views,0) AS views,"
            " po.images,po.sort_ts,"
            " lower(COALESCE(po.text,'')||' '||COALESCE(po.content,'')) AS body"
            " FROM posts po JOIN platforms pl ON pl.id=po.platform_id"
            " LEFT JOIN sources src ON src.id=po.source_id LEFT JOIN regions r ON r.id=po.region_id"
            " WHERE ($2::text='' OR pl.key=$2::text) AND ($3::text='' OR po.origin=$3::text)"
            " AND ($6::text=''"
            " OR ($6::text='official_dsns' AND COALESCE(src.is_official_dsns,false))"
            " OR ($6::text='external' AND NOT COALESCE(src.is_official_dsns,false)))"
            // Keep the DSNS relevance predicate directly on posts.text so
            // PostgreSQL can use the live trigram index before windowing.
            " AND po.text ~* '(дснс|гсчс|рятуваль|надзвичайн|пожежник|спасател|emergency service)'"
            " AND ($5::text=''"
            " OR ($5::text='criticism' AND po.text ~* '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)')"
            " OR ($5::text='praise' AND po.text !~* '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)'"
            " AND po.text ~* '(дяку|подяк|молодц|геро|пишаєм|вдячн|спасиб|hero|thank)')"
            " OR ($5::text='other' AND po.text !~* '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)'"
            " AND po.text !~* '(дяку|подяк|молодц|геро|пишаєм|вдячн|спасиб|hero|thank)'))"
            // Older Threads collection could ingest suggested posts rendered
            // on a profile page. Keep those already-stored foreign posts out
            // of every feed by requiring the permalink owner to match the
            // configured source profile; unverifiable rows fail closed.
            " AND (pl.key<>'threads' OR lower(substring(po.post_url from '/@([^/?#]+)/post/'))="
            " lower(substring(COALESCE(po.source_page_url,src.url) from '/@([^/?#]+)')))"
            "), classified AS ("
            " SELECT base.*,CASE"
            " WHEN body !~ '(дснс|гсчс|рятуваль|надзвичайн|пожежник|спасател|emergency service)' THEN ''"
            " WHEN body ~ '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)' THEN 'criticism'"
            " WHEN body ~ '(дяку|подяк|молодц|геро|пишаєм|вдячн|спасиб|hero|thank)' THEN 'praise'"
            " ELSE 'other' END AS mention_tone"
            " FROM base"
            "), filtered AS ("
            " SELECT classified.*,row_number() OVER (PARTITION BY platform ORDER BY sort_ts DESC,id DESC) AS platform_rank"
            " FROM classified"
            ") SELECT id::text,platform,origin,legacy_source_id,source_name,region,published_at::text,"
            " text,content,post_url,likes,comments_count,views,images::text,mention_tone"
            " FROM filtered"
            // A DSNS-mention query across all platforms is deliberately
            // round-robin ranked: high-volume sources do not crowd smaller
            // networks out of the result set.
            " ORDER BY CASE WHEN $4::text='dsns' AND $2::text='' THEN platform_rank ELSE 0 END,"
            " sort_ts DESC,id DESC LIMIT $1::int");
        const auto onSuccess = [callback, requireText, limit](const orm::Result &rows) {
                Json::Value items(Json::arrayValue);
                for (const auto &row : rows)
                {
                    if (items.size() >= static_cast<Json::ArrayIndex>(limit)) break;
                    Json::Value item;
                    item["id"]=row["id"].as<std::string>(); item["platform"]=row["platform"].as<std::string>();
                    item["origin"]=row["origin"].as<std::string>(); item["sourceId"]=row["legacy_source_id"].as<std::string>();
                    item["sourceName"]=row["source_name"].as<std::string>(); item["region"]=row["region"].as<std::string>();
                    item["publishedAt"]=row["published_at"].as<std::string>();
                    const bool isVk = row["platform"].as<std::string>() == "vk";
                    const std::string postText = isVk
                        ? vkAuthoredText(row["text"].as<std::string>())
                        : row["text"].as<std::string>();
                    const std::string rawContent = row["content"].as<std::string>();
                    const std::string content = isVk ? vkAuthoredText(rawContent) : rawContent;
                    if (requireText && trimWhitespace(postText).empty() &&
                        trimWhitespace(content).empty()) continue;
                    item["text"]=postText; item["title"]=content.empty()?postText:content;
                    item["url"]=row["post_url"].as<std::string>(); item["likes"]=row["likes"].as<int>();
                    item["commentsCount"]=row["comments_count"].as<int>(); item["views"]=row["views"].as<int>();
                    item["mentionTone"]=row["mention_tone"].as<std::string>();
                    item["imageUrl"] = Json::nullValue;
                    Json::Value images; Json::CharReaderBuilder b; std::string errors; std::istringstream in(row["images"].as<std::string>());
                    if (Json::parseFromStream(b,in,&images,&errors) && images.isArray() && !images.empty())
                        item["imageUrl"] = images[0].isString() ? images[0] : images[0].get("url", Json::nullValue);
                    items.append(item);
                }
                Json::Value json; json["posts"] = items; callback(jsonResponse(json));
            };
        const auto onError = [callback](const orm::DrogonDbException &) {
            callback(response("unavailable", "database_query_failed", k503ServiceUnavailable));
        };

        if (balance.empty())
            g_db->execSqlAsync(selectPosts, onSuccess, onError,
                               limit, platform, origin, topic, tone, publisher);
        else
            g_db->execSqlAsync(selectPosts, onSuccess, onError,
                               limit, platform, origin, topic, tone, publisher, balance,
                               requireTextParameter);
    }, {Get});

    // Aggregate KPIs/breakdowns for the Analytics page. /api/posts above is
    // capped at 1000 rows (fine for a recency feed or "top N" list), which
    // would silently undercount "Всього постів"/"Тональність (загальна)"/etc.
    // once a filter's true match count exceeds that cap. This runs the same
    // origin/region/mention_tone logic as /api/posts but via SQL
    // COUNT/SUM/GROUP BY over the *entire* matching row set, so KPI totals
    // and the day-by-day/platform/region breakdowns stay correct regardless
    // of how many posts match.
    app().registerHandler("/api/analytics/summary", [](const HttpRequestPtr &req,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        std::string origin = req->getParameter("origin");
        if (!origin.empty() && origin != "general" && origin != "dsns")
        {
            callback(response("error", "invalid_origin", k400BadRequest));
            return;
        }
        const std::string region = req->getParameter("region");
        const std::string from = req->getParameter("from");
        const std::string to = req->getParameter("to");

        // Reads the persisted po.mention_tone (migration 0040) instead of
        // recomputing it via a per-row regex over text/content: measured at
        // 22-56s over 170k-520k rows on production before this, because the
        // trigram index on the bare `text` column doesn't cover the
        // concatenated text||content expression, forcing a sequential scan
        // that decompresses every row's TOASTed text. A plain column read
        // avoids that entirely.
        static const char *kSummarySql =
            "WITH classified AS ("
            " SELECT pl.key AS platform,COALESCE(po.legacy_source_id,'') AS legacy_source_id,"
            " COALESCE(po.source_name,'') AS source_name,COALESCE(r.name,'') AS region,"
            " COALESCE(po.published_at,po.collected_at) AS ts,COALESCE(po.views,0) AS views,"
            " COALESCE(po.likes,0) AS likes,COALESCE(po.comments_count,0) AS comments_count,"
            " COALESCE(po.mention_tone,'') AS mention_tone"
            " FROM posts po JOIN platforms pl ON pl.id=po.platform_id"
            " LEFT JOIN sources src ON src.id=po.source_id LEFT JOIN regions r ON r.id=po.region_id"
            " WHERE ($1::text='' OR po.origin=$1::text) AND ($2::text='' OR COALESCE(r.name,'')=$2::text)"
            " AND (NULLIF($3,'')::timestamptz IS NULL OR COALESCE(po.published_at,po.collected_at)>=NULLIF($3,'')::timestamptz)"
            " AND (NULLIF($4,'')::timestamptz IS NULL OR COALESCE(po.published_at,po.collected_at)<=NULLIF($4,'')::timestamptz)"
            " AND (pl.key<>'threads' OR lower(substring(po.post_url from '/@([^/?#]+)/post/'))="
            " lower(substring(COALESCE(po.source_page_url,src.url) from '/@([^/?#]+)')))"
            "), totals AS ("
            " SELECT count(*)::bigint AS total_posts,"
            " count(DISTINCT (CASE WHEN legacy_source_id<>'' THEN legacy_source_id ELSE source_name END))::bigint AS unique_sources,"
            " COALESCE(sum(views),0)::bigint AS total_views,COALESCE(sum(likes+comments_count),0)::bigint AS total_interactions,"
            " COALESCE(sum(comments_count),0)::bigint AS total_comments,"
            " count(*) FILTER (WHERE mention_tone='praise')::bigint AS positive,"
            " count(*) FILTER (WHERE mention_tone='other')::bigint AS neutral,"
            " count(*) FILTER (WHERE mention_tone='criticism')::bigint AS negative"
            " FROM classified"
            "), by_day AS ("
            " SELECT to_char(ts,'YYYY-MM-DD') AS date,count(*)::bigint AS posts,"
            " count(*) FILTER (WHERE mention_tone='praise')::bigint AS positive,"
            " count(*) FILTER (WHERE mention_tone='other')::bigint AS neutral,"
            " count(*) FILTER (WHERE mention_tone='criticism')::bigint AS negative"
            " FROM classified GROUP BY 1 ORDER BY 1"
            "), by_platform AS ("
            " SELECT platform,count(*)::bigint AS count FROM classified GROUP BY 1 ORDER BY count DESC"
            "), by_region AS ("
            " SELECT region,count(*)::bigint AS count FROM classified WHERE region<>'' GROUP BY 1 ORDER BY count DESC"
            ") SELECT"
            " (SELECT row_to_json(totals) FROM totals) AS totals_json,"
            " COALESCE((SELECT json_agg(by_day) FROM by_day),'[]') AS by_day_json,"
            " COALESCE((SELECT json_agg(by_platform) FROM by_platform),'[]') AS by_platform_json,"
            " COALESCE((SELECT json_agg(by_region) FROM by_region),'[]') AS by_region_json";

        g_db->execSqlAsync(
            kSummarySql,
            [callback](const orm::Result &rows) {
                auto parseJsonColumn = [](const std::string &text, Json::ValueType fallbackType) {
                    Json::Value parsed(fallbackType);
                    Json::CharReaderBuilder b; std::string errors; std::istringstream in(text);
                    Json::parseFromStream(b, in, &parsed, &errors);
                    return parsed;
                };

                Json::Value result;
                Json::Value totals(Json::objectValue);
                Json::Value byDay(Json::arrayValue), byPlatform(Json::arrayValue), byRegion(Json::arrayValue);
                if (!rows.empty())
                {
                    const auto &row = rows[0];
                    totals = parseJsonColumn(row["totals_json"].as<std::string>(), Json::objectValue);
                    byDay = parseJsonColumn(row["by_day_json"].as<std::string>(), Json::arrayValue);
                    byPlatform = parseJsonColumn(row["by_platform_json"].as<std::string>(), Json::arrayValue);
                    byRegion = parseJsonColumn(row["by_region_json"].as<std::string>(), Json::arrayValue);
                }

                result["totalPosts"] = totals.get("total_posts", 0);
                result["uniqueSources"] = totals.get("unique_sources", 0);
                result["totalViews"] = totals.get("total_views", 0);
                result["totalInteractions"] = totals.get("total_interactions", 0);
                result["totalComments"] = totals.get("total_comments", 0);
                Json::Value sentiment;
                sentiment["positive"] = totals.get("positive", 0);
                sentiment["neutral"] = totals.get("neutral", 0);
                sentiment["negative"] = totals.get("negative", 0);
                result["sentiment"] = sentiment;

                Json::Value postsByDay(Json::arrayValue);
                for (const auto &d : byDay)
                {
                    Json::Value item;
                    item["date"] = d.get("date", "");
                    item["posts"] = d.get("posts", 0);
                    item["positive"] = d.get("positive", 0);
                    item["neutral"] = d.get("neutral", 0);
                    item["negative"] = d.get("negative", 0);
                    postsByDay.append(item);
                }
                result["postsByDay"] = postsByDay;

                Json::Value platformBreakdown(Json::arrayValue);
                for (const auto &p : byPlatform)
                {
                    Json::Value item;
                    item["platform"] = p.get("platform", "");
                    item["count"] = p.get("count", 0);
                    platformBreakdown.append(item);
                }
                result["platformBreakdown"] = platformBreakdown;

                Json::Value regionBreakdown(Json::arrayValue);
                for (const auto &r : byRegion)
                {
                    Json::Value item;
                    item["region"] = r.get("region", "");
                    item["count"] = r.get("count", 0);
                    regionBreakdown.append(item);
                }
                result["regionBreakdown"] = regionBreakdown;

                callback(jsonResponse(result));
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
            origin, region, from, to);
    }, {Get});

    // Full source list (all platforms/origins) for the "Джерела" page — the
    // full ~1,100+ rows already migrated into the live `sources` table, not
    // paginated server-side; the page filters/paginates client-side same as
    // it already does for the fixture it replaces.
    app().registerHandler("/api/sources", [](const HttpRequestPtr &,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        g_db->execSqlAsync(
            "SELECT s.id::text,s.name,s.url,pl.key AS platform,COALESCE(r.name,'') AS region,"
            "s.origin,s.enabled,"
            "count(po.id) AS post_count_total,"
            "count(po.id) FILTER (WHERE po.published_at > now() - interval '24 hours') AS post_count_24h,"
            "COALESCE(max(po.published_at)::text,'') AS last_post_at "
            "FROM sources s JOIN platforms pl ON pl.id=s.platform_id"
            " LEFT JOIN regions r ON r.id=s.region_id"
            " LEFT JOIN posts po ON po.source_id=s.id"
            " GROUP BY s.id,pl.key,r.name ORDER BY s.name",
            [callback](const orm::Result &rows) {
                Json::Value items(Json::arrayValue);
                for (const auto &row : rows)
                {
                    Json::Value item;
                    item["id"]=row["id"].as<std::string>(); item["name"]=row["name"].as<std::string>();
                    item["url"]=row["url"].as<std::string>(); item["platform"]=row["platform"].as<std::string>();
                    item["region"]=row["region"].as<std::string>(); item["origin"]=row["origin"].as<std::string>();
                    item["status"]=row["enabled"].as<bool>() ? "active" : "inactive";
                    item["postCountTotal"]=row["post_count_total"].as<long>();
                    item["postCount24h"]=row["post_count_24h"].as<long>();
                    item["lastPostAt"]=row["last_post_at"].as<std::string>();
                    items.append(item);
                }
                Json::Value json; json["sources"] = items; json["total"] = static_cast<int>(items.size());
                callback(jsonResponse(json));
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
    }, {Get});

    // Keywords: real CRUD + real substring matching against `posts`, persisted
    // into `post_keywords` (the schema's own intended join table, previously
    // never populated by anything — see migrations/postgres/0004_taxonomy.sql
    // in the old repo). Deliberately scoped to existing posts only: this does
    // NOT hook into the collector/normalizer pipeline, so newly-collected
    // posts aren't matched automatically — "Перерахувати" (recalculate) below
    // re-scans everything on demand instead.
    app().registerHandler("/api/keywords", [](const HttpRequestPtr &,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        g_db->execSqlAsync(
            "SELECT id::text,name FROM keyword_groups ORDER BY name",
            [callback](const orm::Result &groupRows) {
                Json::Value groups(Json::arrayValue);
                size_t idx = 0;
                for (const auto &row : groupRows)
                {
                    Json::Value g;
                    g["id"] = row["id"].as<std::string>();
                    g["name"] = row["name"].as<std::string>();
                    g["color"] = kGroupColors[idx % kGroupColors.size()];
                    groups.append(g);
                    ++idx;
                }
                g_db->execSqlAsync(
                    "SELECT k.id::text,k.value,COALESCE(k.group_id::text,'') AS group_id,"
                    "COALESCE(kg.name,'') AS group_name,COALESCE(k.category,'') AS category,"
                    "COALESCE(k.language,'uk') AS language,k.is_active,k.created_at::text,"
                    "(SELECT count(*) FROM post_keywords pk WHERE pk.keyword_id=k.id) AS matches_total "
                    "FROM keywords k LEFT JOIN keyword_groups kg ON kg.id=k.group_id ORDER BY k.created_at DESC",
                    [callback, groups](const orm::Result &rows) {
                        Json::Value keywords(Json::arrayValue);
                        for (const auto &row : rows)
                        {
                            Json::Value k;
                            k["id"] = row["id"].as<std::string>();
                            k["word"] = row["value"].as<std::string>();
                            k["groupId"] = row["group_id"].as<std::string>();
                            k["groupName"] = row["group_name"].as<std::string>();
                            k["category"] = row["category"].as<std::string>();
                            k["language"] = row["language"].as<std::string>();
                            k["status"] = row["is_active"].as<bool>() ? "active" : "inactive";
                            k["createdAt"] = row["created_at"].as<std::string>();
                            k["matchesTotal"] = row["matches_total"].as<long>();
                            keywords.append(k);
                        }
                        Json::Value categories(Json::arrayValue);
                        for (const auto &c : kKeywordCategories)
                        {
                            Json::Value cc;
                            cc["id"] = c.id;
                            cc["label"] = c.label;
                            categories.append(cc);
                        }
                        Json::Value json;
                        json["groups"] = groups;
                        json["categories"] = categories;
                        json["keywords"] = keywords;
                        callback(jsonResponse(json));
                    },
                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
    }, {Get});

    app().registerHandler("/api/keywords", [](const HttpRequestPtr &req,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        auto body = req->getJsonObject();
        const std::string word = body ? (*body)["word"].asString() : "";
        if (word.empty()) { callback(response("error", "word_required", k400BadRequest)); return; }
        const std::string groupId = (body && (*body).isMember("groupId")) ? (*body)["groupId"].asString() : "";
        const std::string category = (body && (*body).isMember("category")) ? (*body)["category"].asString() : "";
        const std::string language = (body && (*body).isMember("language")) ? (*body)["language"].asString() : "uk";

        g_db->execSqlAsync(
            "INSERT INTO keywords(value,group_id,category,language) "
            "VALUES($1,NULLIF($2,'')::uuid,NULLIF($3,''),$4) "
            "ON CONFLICT (value) DO NOTHING RETURNING id::text,created_at::text",
            [callback, word](const orm::Result &rows) {
                if (rows.empty()) { callback(response("error", "keyword_exists", k409Conflict)); return; }
                const std::string id = rows[0]["id"].as<std::string>();
                const std::string createdAt = rows[0]["created_at"].as<std::string>();
                g_db->execSqlAsync(
                    "INSERT INTO post_keywords(post_id,keyword_id) "
                    "SELECT p.id,$1::uuid FROM posts p WHERE p.text ILIKE '%'||$2||'%' ESCAPE '\\' "
                    "ON CONFLICT DO NOTHING",
                    [callback, id, createdAt](const orm::Result &) {
                        g_db->execSqlAsync(
                            "SELECT count(*) AS c FROM post_keywords WHERE keyword_id=$1::uuid",
                            [callback, id, createdAt](const orm::Result &countRows) {
                                Json::Value j;
                                j["id"] = id;
                                j["createdAt"] = createdAt;
                                j["matchesTotal"] = countRows.empty() ? 0L : countRows[0]["c"].as<long>();
                                callback(jsonResponse(j));
                            },
                            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                            id);
                    },
                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                    id, escapeLikePattern(word));
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
            word, groupId, category, language);
    }, {Post});

    app().registerHandler("/api/keywords/{id}", [](const HttpRequestPtr &req,
        std::function<void(const HttpResponsePtr &)> &&callback, const std::string &id) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        auto body = req->getJsonObject();
        const std::string word = body ? (*body)["word"].asString() : "";
        if (word.empty()) { callback(response("error", "word_required", k400BadRequest)); return; }
        const std::string groupId = (body && (*body).isMember("groupId")) ? (*body)["groupId"].asString() : "";
        const std::string category = (body && (*body).isMember("category")) ? (*body)["category"].asString() : "";
        const std::string language = (body && (*body).isMember("language")) ? (*body)["language"].asString() : "uk";
        const bool isActive = !(body && (*body).isMember("status")) || (*body)["status"].asString() != "inactive";

        g_db->execSqlAsync(
            "UPDATE keywords SET value=$1,group_id=NULLIF($2,'')::uuid,category=NULLIF($3,''),"
            "language=$4,is_active=$5 WHERE id=$6::uuid RETURNING id",
            [callback, id, word](const orm::Result &rows) {
                if (rows.empty()) { callback(response("error", "not_found", k404NotFound)); return; }
                g_db->execSqlAsync(
                    "DELETE FROM post_keywords WHERE keyword_id=$1::uuid",
                    [callback, id, word](const orm::Result &) {
                        g_db->execSqlAsync(
                            "INSERT INTO post_keywords(post_id,keyword_id) "
                            "SELECT p.id,$1::uuid FROM posts p WHERE p.text ILIKE '%'||$2||'%' ESCAPE '\\' "
                            "ON CONFLICT DO NOTHING",
                            [callback, id](const orm::Result &) {
                                g_db->execSqlAsync(
                                    "SELECT count(*) AS c FROM post_keywords WHERE keyword_id=$1::uuid",
                                    [callback](const orm::Result &countRows) {
                                        Json::Value j;
                                        j["matchesTotal"] = countRows.empty() ? 0L : countRows[0]["c"].as<long>();
                                        callback(jsonResponse(j));
                                    },
                                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                                    id);
                            },
                            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                            id, escapeLikePattern(word));
                    },
                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                    id);
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
            word, groupId, category, language, isActive, id);
    }, {Put});

    app().registerHandler("/api/keywords/{id}", [](const HttpRequestPtr &,
        std::function<void(const HttpResponsePtr &)> &&callback, const std::string &id) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        g_db->execSqlAsync(
            "DELETE FROM keywords WHERE id=$1::uuid",
            [callback](const orm::Result &) { callback(response("ok", nullptr, k200OK)); },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
            id);
    }, {Delete});

    app().registerHandler("/api/keyword-groups", [](const HttpRequestPtr &req,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        auto body = req->getJsonObject();
        const std::string name = body ? (*body)["name"].asString() : "";
        if (name.empty()) { callback(response("error", "name_required", k400BadRequest)); return; }
        g_db->execSqlAsync(
            "INSERT INTO keyword_groups(name) VALUES($1) ON CONFLICT (name) DO NOTHING RETURNING id::text",
            [callback, name](const orm::Result &rows) {
                if (rows.empty()) { callback(response("error", "group_exists", k409Conflict)); return; }
                Json::Value j;
                j["id"] = rows[0]["id"].as<std::string>();
                j["name"] = name;
                callback(jsonResponse(j));
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
            name);
    }, {Post});

    // Deleting a group unassigns its keywords rather than deleting them
    // (matches the frontend's "Без групи" fallback for an empty groupId).
    app().registerHandler("/api/keyword-groups/{id}", [](const HttpRequestPtr &,
        std::function<void(const HttpResponsePtr &)> &&callback, const std::string &id) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        g_db->execSqlAsync(
            "UPDATE keywords SET group_id=NULL WHERE group_id=$1::uuid",
            [callback, id](const orm::Result &) {
                g_db->execSqlAsync(
                    "DELETE FROM keyword_groups WHERE id=$1::uuid",
                    [callback](const orm::Result &) { callback(response("ok", nullptr, k200OK)); },
                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                    id);
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
            id);
    }, {Delete});

    // Real "Перерахувати" (recalculate) — re-scans every existing post
    // against every keyword and rebuilds post_keywords from scratch. This is
    // the only way, short of a pipeline hook, to pick up posts collected
    // after a keyword was created.
    app().registerHandler("/api/keywords/recalculate", [](const HttpRequestPtr &,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        g_db->execSqlAsync(
            "DELETE FROM post_keywords",
            [callback](const orm::Result &) {
                g_db->execSqlAsync(
                    "INSERT INTO post_keywords(post_id,keyword_id) "
                    "SELECT p.id,k.id FROM posts p JOIN keywords k "
                    "ON p.text ILIKE '%'||replace(replace(replace(k.value,'\\','\\\\'),'%','\\%'),'_','\\_')||'%' ESCAPE '\\' "
                    "ON CONFLICT DO NOTHING",
                    [callback](const orm::Result &) {
                        g_db->execSqlAsync(
                            "SELECT count(*) AS c FROM post_keywords",
                            [callback](const orm::Result &countRows) {
                                Json::Value j;
                                j["totalMatches"] = countRows.empty() ? 0L : countRows[0]["c"].as<long>();
                                callback(jsonResponse(j));
                            },
                            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
                    },
                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); });
    }, {Post});

    // Real comments (118k+ rows already collected, previously never exposed
    // via any API — CommentsPage.cpp read fixtures/comments.json instead).
    // No sentiment analysis has ever run over these, so "sentiment" is
    // returned empty rather than fabricated — matches this app's convention
    // of an honest empty/pending state instead of faking AI output.
    app().registerHandler("/api/comments", [](const HttpRequestPtr &req,
        std::function<void(const HttpResponsePtr &)> &&callback) {
        if (!g_db) { callback(response("unavailable", g_configError.c_str(), k503ServiceUnavailable)); return; }
        const int limit = std::clamp([&] { try { return std::stoi(req->getParameter("limit")); } catch (...) { return 300; } }(), 1, 1000);
        // Capped per-platform (via ROW_NUMBER) rather than one global
        // "ORDER BY recency LIMIT N" — platforms with far higher comment
        // volume (facebook, youtube) would otherwise crowd lower-volume
        // platforms (e.g. threads) out of the window entirely, even though
        // they have real comments, just fewer/less recent ones.
        g_db->execSqlAsync(
            // published_at is frequently null (not every collector captures
            // it); fall back to collected_at, same convention as /api/posts.
            // Scoped to origin='dsns' only — comments on general/OSINT posts
            // are out of scope here, per explicit request.
            "SELECT id::text,post_id::text,platform,author_name,text,published_at,likes_count FROM ("
            "  SELECT c.id,c.post_id,c.platform,COALESCE(c.author_name,'') AS author_name,"
            "  c.text,COALESCE(c.published_at,c.collected_at) AS published_at,COALESCE(c.likes_count,0) AS likes_count,"
            "  row_number() OVER (PARTITION BY c.platform ORDER BY COALESCE(c.published_at,c.collected_at) DESC) AS rn"
            "  FROM comments c JOIN posts po ON po.id=c.post_id"
            "  WHERE po.origin='dsns'"
            ") sub WHERE rn <= 50 ORDER BY published_at DESC LIMIT $1::int",
            [callback](const orm::Result &rows) {
                Json::Value items(Json::arrayValue);
                std::string idArray = "{";
                bool first = true;
                for (const auto &row : rows)
                {
                    Json::Value item;
                    item["id"] = row["id"].as<std::string>();
                    const std::string postId = row["post_id"].as<std::string>();
                    item["postId"] = postId;
                    item["platform"] = row["platform"].as<std::string>();
                    item["author"] = row["author_name"].as<std::string>();
                    item["text"] = row["text"].as<std::string>();
                    item["sentiment"] = "";
                    item["publishedAt"] = row["published_at"].as<std::string>();
                    item["likes"] = row["likes_count"].as<long>();
                    items.append(item);
                    if (!first) idArray += ",";
                    idArray += "\"" + postId + "\"";
                    first = false;
                }
                idArray += "}";

                // Fetch the exact posts these comments belong to (not an
                // independent "latest N posts" window, which in general
                // shares almost no rows with "latest N comments" — comments
                // get collected on their own schedule, often against older
                // posts) — this is what makes comment↔post linkage on the
                // frontend actually resolve instead of silently dropping
                // every comment whose post fell outside an unrelated window.
                g_db->execSqlAsync(
                    "SELECT po.id::text,pl.key AS platform,po.origin,po.legacy_source_id,po.source_name,"
                    "COALESCE(r.name,'') AS region,COALESCE(po.published_at,po.collected_at)::text AS published_at,"
                    "po.text,po.content,po.post_url,COALESCE(po.likes,0) AS likes,COALESCE(po.comments_count,0) AS comments_count,"
                    "COALESCE(po.views,0) AS views,po.images::text FROM posts po JOIN platforms pl ON pl.id=po.platform_id"
                    " LEFT JOIN regions r ON r.id=po.region_id WHERE po.id = ANY($1::uuid[])",
                    [callback, items](const orm::Result &postRows) {
                        Json::Value posts(Json::arrayValue);
                        for (const auto &row : postRows)
                        {
                            Json::Value item;
                            item["id"]=row["id"].as<std::string>(); item["platform"]=row["platform"].as<std::string>();
                            item["origin"]=row["origin"].as<std::string>(); item["sourceId"]=row["legacy_source_id"].as<std::string>();
                            item["sourceName"]=row["source_name"].as<std::string>(); item["region"]=row["region"].as<std::string>();
                            item["publishedAt"]=row["published_at"].as<std::string>(); item["text"]=row["text"].as<std::string>();
                            const std::string content=row["content"].as<std::string>(); item["title"]=content.empty()?row["text"].as<std::string>():content;
                            item["url"]=row["post_url"].as<std::string>(); item["likes"]=row["likes"].as<int>();
                            item["commentsCount"]=row["comments_count"].as<int>(); item["views"]=row["views"].as<int>();
                            item["imageUrl"] = Json::nullValue;
                            Json::Value images; Json::CharReaderBuilder b; std::string errors; std::istringstream in(row["images"].as<std::string>());
                            if (Json::parseFromStream(b,in,&images,&errors) && images.isArray() && !images.empty())
                                item["imageUrl"] = images[0].isString() ? images[0] : images[0].get("url", Json::nullValue);
                            posts.append(item);
                        }
                        Json::Value json;
                        json["comments"] = items;
                        json["posts"] = posts;
                        callback(jsonResponse(json));
                    },
                    [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); },
                    idArray);
            },
            [callback](const orm::DrogonDbException &) { callback(response("unavailable", "database_query_failed", k503ServiceUnavailable)); }, limit);
    }, {Get});

}

}  // namespace argus::backend::database
