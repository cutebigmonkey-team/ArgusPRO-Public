#include "PipelineDb.h"
#include "RssNormalize.h"

#include <curl/curl.h>
#include <drogon/drogon.h>
#include <json/json.h>

#include <algorithm>
#include <iostream>
#include <optional>
#include <string>
#include <vector>

using argus::rss::Item;
using argus::rss::atomLink;
using argus::rss::canonicalize;
using argus::rss::isFeed;
using argus::rss::lower;
using argus::rss::parse;
using argus::rss::resolveUrl;
using argus::rss::unescape;

namespace {
struct Feed { std::string url, body; };
size_t writeBody(char *data, size_t size, size_t count, void *target)
{
    static_cast<std::string *>(target)->append(data, size * count);
    return size * count;
}
int abortWhenStopping(void *, curl_off_t, curl_off_t, curl_off_t, curl_off_t)
{
    return argus::pipeline::stopping() ? 1 : 0;
}
bool fetch(const std::string &url, std::string &body, std::string *effectiveUrl = nullptr)
{
    CURL *curl = curl_easy_init();
    if (!curl) return false;
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, writeBody);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &body);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 5L);
    curl_easy_setopt(curl, CURLOPT_TIMEOUT, 12L);
    curl_easy_setopt(curl, CURLOPT_MAXFILESIZE_LARGE, static_cast<curl_off_t>(5 * 1024 * 1024));
    curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "http,https");
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "Mozilla/5.0 (compatible; ArgusPRO-RSS/1.0)");
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, abortWhenStopping);
    curl_slist *headers = nullptr;
    headers = curl_slist_append(headers,
        "Accept: application/rss+xml, application/atom+xml, application/xml, text/xml, text/html;q=0.8, */*;q=0.1");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    const CURLcode result = curl_easy_perform(curl);
    long code = 0;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &code);
    char *effective = nullptr;
    curl_easy_getinfo(curl, CURLINFO_EFFECTIVE_URL, &effective);
    if (effectiveUrl != nullptr && effective != nullptr) *effectiveUrl = effective;
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    return result == CURLE_OK && code >= 200 && code < 300;
}
std::optional<std::string> alternateFeedUrl(const std::string &html, const std::string &base)
{
    const std::string folded = lower(html);
    size_t cursor = 0;
    while ((cursor = folded.find("<link", cursor)) != std::string::npos)
    {
        const size_t end = folded.find('>', cursor);
        if (end == std::string::npos) break;
        const std::string foldedTag = folded.substr(cursor, end - cursor + 1);
        if (foldedTag.find("rss") != std::string::npos || foldedTag.find("atom") != std::string::npos)
        {
            const size_t href = foldedTag.find("href=");
            if (href != std::string::npos && cursor + href + 5 < html.size())
            {
                size_t value = cursor + href + 5;
                const char quote = html[value];
                if (quote == '\'' || quote == '"')
                {
                    const size_t close = html.find(quote, ++value);
                    if (close != std::string::npos)
                        return resolveUrl(base, unescape(html.substr(value, close - value)));
                }
            }
        }
        cursor = end + 1;
    }
    return std::nullopt;
}
std::optional<Feed> discoverFeed(const std::string &baseUrl)
{
    if (argus::pipeline::stopping()) return std::nullopt;
    std::string homepage, effectiveBase = baseUrl;
    if (fetch(baseUrl, homepage, &effectiveBase))
    {
        if (isFeed(homepage)) return Feed{effectiveBase, std::move(homepage)};
        if (const auto alternate = alternateFeedUrl(homepage, effectiveBase))
        {
            std::string body, effective = *alternate;
            if (fetch(*alternate, body, &effective) && isFeed(body))
                return Feed{effective, std::move(body)};
        }
    }

    static const std::vector<std::string> commonPaths = {
        "/?format=feed&type=rss", "/?format=feed&type=atom",
        "/index.php?format=feed&type=rss", "/rss", "/rss.xml",
        "/feed", "/feed.xml", "/feed/rss", "/rss/ostanni-novini"};
    std::vector<std::string> paths = commonPaths;
    if (baseUrl.find("dsns.gov.ua") != std::string::npos)
        std::rotate(paths.begin(), paths.end() - 1, paths.end());
    for (const auto &path : paths)
    {
        if (argus::pipeline::stopping()) return std::nullopt;
        const std::string candidate = resolveUrl(effectiveBase, path);
        std::string body, effective = candidate;
        if (fetch(candidate, body, &effective) && isFeed(body))
            return Feed{effective, std::move(body)};
    }
    return std::nullopt;
}
}  // namespace

int main()
{
    using namespace argus::pipeline;
    std::cout << std::unitbuf;
    std::cerr << std::unitbuf;
    installSignalHandlers();
    curl_global_init(CURL_GLOBAL_DEFAULT);
    const bool writer = envString("ARGUS_COLLECTOR_MODE", "shadow") == "writer";
    const std::string worker = envString("ARGUS_WORKER_ID", "rss-vm");
    const int batch = envInt("ARGUS_COLLECTOR_BATCH_SIZE", 5, 1, 20);
    const int maxItems = envInt("ARGUS_COLLECTOR_MAX_ITEMS", 20, 1, 50);
    ProgressWatchdog watchdog(
        "rss_collector", envInt("ARGUS_PROGRESS_TIMEOUT_SECONDS", 180, 30, 600));
    try
    {
        auto db = connectDatabase();
        db->execSqlSync(
            "UPDATE argus_jobs j SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
            " available_at=now(),locked_at=NULL,locked_by=NULL,last_error='stale_collector_lock'"
            " FROM sources s JOIN platforms p ON p.id=s.platform_id"
            " WHERE j.source_id=s.id AND p.key='rss' AND j.job_type='collect_source'"
            " AND (($1 AND j.payload->>'mode'='shadow')"
            " OR (NOT $1 AND COALESCE(j.payload->>'mode','writer')<>'shadow'))"
            " AND j.status='processing'"
            " AND (j.locked_by=$2 OR j.locked_at<now()-interval '15 minutes')",
            !writer, worker);
        std::cout << "{\"component\":\"rss_collector\",\"event\":\"started\",\"mode\":\""
                  << (writer ? "writer" : "shadow") << "\"}\n";
        while (!stopping())
        {
            watchdog.heartbeat();
            auto jobs = db->execSqlSync(
                "WITH picked AS (SELECT j.id,j.source_id,s.origin,s.name,s.url FROM argus_jobs j"
                " JOIN sources s ON s.id=j.source_id JOIN platforms p ON p.id=s.platform_id"
                " LEFT JOIN collector_health ch ON ch.source_id=s.id"
                " LEFT JOIN argus_shadow_source_state ss ON ss.source_id=s.id"
                " WHERE j.job_type='collect_source' AND p.key='rss' AND j.status IN ('pending','retry')"
                " AND (($3 AND j.payload->>'mode'='shadow') OR (NOT $3 AND COALESCE(j.payload->>'mode','writer')<>'shadow'))"
                " AND j.available_at<=now()"
                " ORDER BY CASE WHEN $3 THEN ss.last_checked_at ELSE ch.last_checked_at END"
                " ASC NULLS FIRST,j.priority DESC,j.created_at"
                " FOR UPDATE OF j SKIP LOCKED LIMIT $1::int)"
                " UPDATE argus_jobs j SET status='processing',locked_at=now(),locked_by=$2,attempts=attempts+1"
                " FROM picked WHERE j.id=picked.id RETURNING j.id::text,picked.source_id::text,picked.origin,picked.name,picked.url,j.attempts,j.max_attempts",
                batch, worker, !writer);
            if (jobs.empty()) { interruptibleWait(3); continue; }
            for (const auto &row : jobs)
            {
                if (stopping()) break;
                watchdog.heartbeat();
                const std::string jobId = row["id"].as<std::string>();
                try
                {
                    const auto feed = discoverFeed(row["url"].as<std::string>());
                    watchdog.heartbeat();
                    if (stopping()) break;
                    if (!feed) throw std::runtime_error("feed discovery failed");
                    const auto items = parse(feed->body, static_cast<size_t>(maxItems));
                    watchdog.heartbeat();
                    if (items.empty()) throw std::runtime_error("feed contained no recognized items");
                    bool interrupted = false;
                    for (const auto &item : items)
                    {
                        if (stopping())
                        {
                            interrupted = true;
                            break;
                        }
                        watchdog.heartbeat();
                        Json::Value payload;
                        payload["origin"] = row["origin"].as<std::string>();
                        const std::string canonicalUrl = canonicalize(resolveUrl(feed->url, item.link));
                        if (canonicalUrl.rfind("http://", 0) != 0 && canonicalUrl.rfind("https://", 0) != 0)
                            continue;
                        payload["post_url"] = canonicalUrl;
                        payload["text"] = item.title;
                        payload["published_at"] = item.published;
                        payload["canonical_key"] = "rss:" + canonicalUrl;
                        payload["idempotency_key"] = "rss:" + canonicalUrl;
                        payload["images"] = Json::arrayValue;
                        payload["videos"] = Json::arrayValue;
                        enqueueNormalizationJob(
                            db,
                            row["source_id"].as<std::string>(), payload.toStyledString(),
                            std::string(writer ? "normalize:" : "shadow:normalize:") + canonicalUrl,
                            writer ? "normalize_post" : "shadow_normalize_post");
                    }
                    if (interrupted || stopping()) break;
                    db->execSqlSync("UPDATE argus_jobs SET status='completed',completed_at=now(),locked_at=NULL,locked_by=NULL WHERE id=$1", jobId);
                    if (stopping()) break;
                    if (writer)
                        db->execSqlSync("UPDATE collector_health SET last_status='ok',last_checked_at=now(),last_success_at=now(),consecutive_failures=0,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", row["source_id"].as<std::string>());
                    else
                    {
                        db->execSqlSync("UPDATE argus_shadow_source_state SET last_status='ok',last_checked_at=now(),last_error=NULL,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", row["source_id"].as<std::string>());
                        if (stopping()) break;
                        db->execSqlSync("INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,observed_count) VALUES($1,$2,'collect','ok',$3)", row["source_id"].as<std::string>(), jobId, static_cast<int>(items.size()));
                    }
                    std::cout << "{\"component\":\"rss_collector\",\"event\":\"source_complete\",\"mode\":\""
                              << (writer ? "writer" : "shadow") << "\",\"items\":" << items.size() << "}\n";
                    watchdog.heartbeat();
                }
                catch (const drogon::orm::TimeoutError &)
                {
                    throw;
                }
                catch (const std::exception &)
                {
                    db->execSqlSync("UPDATE argus_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,available_at=now()+make_interval(secs=>LEAST(3600,30*(2^LEAST(attempts,7)))),locked_at=NULL,locked_by=NULL,last_error='collection_failed' WHERE id=$1", jobId);
                    if (stopping()) break;
                    if (writer)
                        db->execSqlSync("UPDATE collector_health SET last_status='failed',last_checked_at=now(),consecutive_failures=consecutive_failures+1,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", row["source_id"].as<std::string>());
                    else
                    {
                        db->execSqlSync("UPDATE argus_shadow_source_state SET last_status='failed',last_checked_at=now(),last_error='collection_failed',claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", row["source_id"].as<std::string>());
                        if (stopping()) break;
                        db->execSqlSync("INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,error_code) VALUES($1,$2,'collect','failed','collection_failed')", row["source_id"].as<std::string>(), jobId);
                    }
                    watchdog.heartbeat();
                }
            }
        }
        curl_global_cleanup();
        return 0;
    }
    catch (const std::exception &)
    {
        std::cerr << "{\"component\":\"rss_collector\",\"event\":\"fatal\",\"reason\":\"database_unavailable\"}\n";
        curl_global_cleanup();
        return 1;
    }
}
