#include "PipelineDb.h"
#include "PlatformAdapters.h"

#include <curl/curl.h>
#include <drogon/drogon.h>
#include <json/json.h>

#include <algorithm>
#include <iostream>
#include <memory>
#include <set>
#include <string>

namespace {

std::shared_ptr<drogon::orm::DbClient> connectWithBackoff()
{
    using namespace argus::pipeline;
    const int attempts = envInt("ARGUS_DB_CONNECT_ATTEMPTS", 6, 1, 10);
    int delay = 1;
    for (int attempt = 1; attempt <= attempts && !stopping(); ++attempt)
    {
        try
        {
            return connectDatabase();
        }
        catch (const std::exception &)
        {
            std::cerr << "{\"component\":\"native_collector\",\"event\":\"db_connect_retry\",\"attempt\":"
                      << attempt << "}\n";
            if (attempt < attempts) interruptibleWait(delay);
            delay = std::min(delay * 2, 30);
        }
    }
    throw std::runtime_error("database_unavailable");
}

Json::Value payloadFor(const argus::collectors::Item &item, const std::string &origin)
{
    Json::Value payload;
    payload["origin"] = origin;
    payload["post_url"] = item.postUrl;
    payload["text"] = item.text;
    payload["published_at"] = item.publishedAt;
    payload["updated_at"] = item.updatedAt;
    payload["canonical_key"] = item.canonicalKey;
    payload["idempotency_key"] = item.canonicalKey;
    payload["images"] = item.images;
    payload["videos"] = item.videos;
    return payload;
}

}  // namespace

int main()
{
    using namespace argus::pipeline;
    std::cout << std::unitbuf;
    std::cerr << std::unitbuf;
    installSignalHandlers();
    curl_global_init(CURL_GLOBAL_DEFAULT);

    const std::string mode = envString("ARGUS_COLLECTOR_MODE", "shadow");
    const bool writer = mode == "writer";
    std::string platform = envString("ARGUS_PLATFORM");
#ifdef ARGUS_COMPILED_PLATFORM
    const std::string compiledPlatform = ARGUS_COMPILED_PLATFORM;
    if (!platform.empty() && platform != compiledPlatform)
    {
        std::cerr << "{\"component\":\"native_collector\",\"event\":\"fatal\",\"reason\":\"platform_mismatch\"}\n";
        return 2;
    }
    platform = compiledPlatform;
#endif
    const std::set<std::string> supported{"telegram", "youtube", "vk", "reddit", "gdelt", "dsns-http"};
    if (mode != "shadow" && !writer)
    {
        std::cerr << "{\"component\":\"native_collector\",\"event\":\"fatal\",\"reason\":\"invalid_collector_mode\"}\n";
        return 2;
    }
    if (!supported.contains(platform))
    {
        std::cerr << "{\"component\":\"native_collector\",\"event\":\"fatal\",\"reason\":\"unsupported_platform\"}\n";
        return 2;
    }

    const std::string worker = envString(
        "ARGUS_WORKER_ID", platform + (writer ? "-writer-vm" : "-shadow-vm"));
    const int batch = envInt("ARGUS_COLLECTOR_BATCH_SIZE", 2, 1, 2);
    const int maxItems = envInt("ARGUS_COLLECTOR_MAX_ITEMS", 20, 1, 50);
    const int idleSeconds = envInt("ARGUS_COLLECTOR_IDLE_SECONDS", 3, 1, 30);
    argus::collectors::HttpOptions http;
    http.connectTimeoutSeconds = envInt("ARGUS_HTTP_CONNECT_TIMEOUT_SECONDS", 5, 1, 20);
    http.totalTimeoutSeconds = envInt("ARGUS_HTTP_TOTAL_TIMEOUT_SECONDS", 15, 2, 60);
    http.maxBytes = static_cast<std::size_t>(envInt("ARGUS_HTTP_MAX_BYTES", 5 * 1024 * 1024,
                                                    64 * 1024, 10 * 1024 * 1024));
    ProgressWatchdog watchdog(
        "native_collector", envInt("ARGUS_PROGRESS_TIMEOUT_SECONDS", 240, 30, 600));

    try
    {
        auto db = connectWithBackoff();
        db->execSqlSync(
            "UPDATE argus_jobs j SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
            " available_at=now(),locked_at=NULL,locked_by=NULL,last_error='stale_collector_lock'"
            " FROM sources s JOIN platforms p ON p.id=s.platform_id"
            " WHERE j.source_id=s.id AND p.key=$1 AND j.job_type='collect_source'"
            " AND (($2::text='shadow' AND j.payload->>'mode'='shadow')"
            " OR ($2::text='writer' AND COALESCE(j.payload->>'mode','writer')='writer'))"
            " AND j.status='processing'"
            " AND (j.locked_by=$3 OR j.locked_at<now()-interval '15 minutes')",
            platform, mode, worker);
        std::cout << "{\"component\":\"native_collector\",\"event\":\"started\",\"platform\":\""
                  << platform << "\",\"mode\":\"" << mode << "\",\"concurrency\":1}\n";

        while (!stopping())
        {
            watchdog.heartbeat();
            // GDELT uses isolated cursor state for each execution mode:
            // migration 0037's JSON cursor in shadow and the production
            // collector_health cursor in writer mode.
            const auto jobs = platform == "gdelt" ? db->execSqlSync(
                "WITH picked AS (SELECT j.id,j.source_id,s.origin,s.url,"
                " COALESCE(CASE WHEN $4::text='shadow' THEN ss.cursor->>'last_end_epoch'"
                " ELSE ch.last_seen_url END,'') AS cursor FROM argus_jobs j"
                " JOIN sources s ON s.id=j.source_id JOIN platforms p ON p.id=s.platform_id"
                " LEFT JOIN argus_shadow_source_state ss ON ss.source_id=s.id"
                " LEFT JOIN collector_health ch ON ch.source_id=s.id"
                " WHERE j.job_type='collect_source' AND p.key=$3 AND j.status IN ('pending','retry')"
                " AND (($4::text='shadow' AND j.payload->>'mode'='shadow')"
                " OR ($4::text='writer' AND COALESCE(j.payload->>'mode','writer')='writer'))"
                " AND j.available_at<=now()"
                " ORDER BY CASE WHEN $4::text='writer' THEN ch.last_checked_at ELSE ss.last_checked_at END"
                " ASC NULLS FIRST,j.priority DESC,j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT $1::int)"
                " UPDATE argus_jobs j SET status='processing',locked_at=now(),locked_by=$2,attempts=attempts+1"
                " FROM picked WHERE j.id=picked.id"
                " RETURNING j.id::text,picked.source_id::text,picked.origin,picked.url,picked.cursor,j.attempts,j.max_attempts",
                batch, worker, platform, mode) : db->execSqlSync(
                "WITH picked AS (SELECT j.id,j.source_id,s.origin,s.url,''::text AS cursor FROM argus_jobs j"
                " JOIN sources s ON s.id=j.source_id JOIN platforms p ON p.id=s.platform_id"
                " LEFT JOIN collector_health ch ON ch.source_id=s.id"
                " LEFT JOIN argus_shadow_source_state ss ON ss.source_id=s.id"
                " WHERE j.job_type='collect_source' AND p.key=$3 AND j.status IN ('pending','retry')"
                " AND (($4::text='shadow' AND j.payload->>'mode'='shadow')"
                " OR ($4::text='writer' AND COALESCE(j.payload->>'mode','writer')='writer'))"
                " AND j.available_at<=now()"
                " ORDER BY CASE WHEN $4::text='writer' THEN ch.last_checked_at ELSE ss.last_checked_at END"
                " ASC NULLS FIRST,j.priority DESC,j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT $1::int)"
                " UPDATE argus_jobs j SET status='processing',locked_at=now(),locked_by=$2,attempts=attempts+1"
                " FROM picked WHERE j.id=picked.id"
                " RETURNING j.id::text,picked.source_id::text,picked.origin,picked.url,picked.cursor,j.attempts,j.max_attempts",
                batch, worker, platform, mode);
            if (jobs.empty())
            {
                interruptibleWait(idleSeconds);
                continue;
            }

            for (const auto &row : jobs)
            {
                watchdog.heartbeat();
                const std::string jobId = row["id"].as<std::string>();
                const std::string sourceId = row["source_id"].as<std::string>();
                const auto result = argus::collectors::collect(platform, row["url"].as<std::string>(),
                                                                static_cast<std::size_t>(maxItems), http,
                                                                row["cursor"].as<std::string>());
                watchdog.heartbeat();
                if (stopping()) break;
                if (!result.errorCode.empty())
                {
                    db->execSqlSync(
                        "UPDATE argus_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
                        " available_at=now()+make_interval(secs=>LEAST(3600,30*(2^LEAST(attempts,7)))),"
                        " locked_at=NULL,locked_by=NULL,last_error=$2 WHERE id=$1", jobId, result.errorCode);
                    if (stopping()) break;
                    if (writer)
                        db->execSqlSync(
                            "UPDATE collector_health SET last_status='failed',last_checked_at=now(),"
                            " consecutive_failures=consecutive_failures+1,claimed_at=NULL,claimed_by=NULL"
                            " WHERE source_id=$1", sourceId);
                    else
                    {
                        db->execSqlSync(
                            "UPDATE argus_shadow_source_state SET last_status='failed',last_checked_at=now(),"
                            " last_error=$2,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1",
                            sourceId, result.errorCode);
                        if (stopping()) break;
                        db->execSqlSync(
                            "INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,error_code)"
                            " VALUES($1,$2,'collect','failed',$3)", sourceId, jobId, result.errorCode);
                    }
                    std::cout << "{\"component\":\"native_collector\",\"event\":\"source_failed\",\"platform\":\""
                              << platform << "\",\"mode\":\"" << mode
                              << "\",\"error_code\":\"" << result.errorCode << "\"}\n";
                    watchdog.heartbeat();
                    continue;
                }

                for (const auto &item : result.items)
                {
                    if (stopping()) break;
                    watchdog.heartbeat();
                    const Json::Value payload = payloadFor(item, row["origin"].as<std::string>());
                    enqueueNormalizationJob(
                        db,
                        sourceId, payload.toStyledString(),
                        std::string(writer ? "normalize:" : "shadow:normalize:") + item.canonicalKey,
                        writer ? "normalize_post" : "shadow_normalize_post");
                }
                if (stopping()) break;
                db->execSqlSync(
                    "UPDATE argus_jobs SET status='completed',completed_at=now(),locked_at=NULL,locked_by=NULL"
                    " WHERE id=$1", jobId);
                if (stopping()) break;
                if (writer && platform == "gdelt")
                    db->execSqlSync(
                        "UPDATE collector_health SET last_status='ok',last_checked_at=now(),last_success_at=now(),"
                        " consecutive_failures=0,last_seen_url=$2,claimed_at=NULL,claimed_by=NULL"
                        " WHERE source_id=$1", sourceId, result.nextCursor);
                else if (writer)
                    db->execSqlSync(
                        "UPDATE collector_health SET last_status='ok',last_checked_at=now(),last_success_at=now(),"
                        " consecutive_failures=0,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", sourceId);
                else if (platform == "gdelt")
                    db->execSqlSync(
                        "UPDATE argus_shadow_source_state SET last_status='ok',last_checked_at=now(),last_error=NULL,"
                        " cursor=jsonb_build_object('last_end_epoch',$2),claimed_at=NULL,claimed_by=NULL"
                        " WHERE source_id=$1", sourceId, result.nextCursor);
                else
                    db->execSqlSync(
                        "UPDATE argus_shadow_source_state SET last_status='ok',last_checked_at=now(),last_error=NULL,"
                        " claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", sourceId);
                if (!writer)
                {
                    if (stopping()) break;
                    db->execSqlSync(
                        "INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,observed_count)"
                        " VALUES($1,$2,'collect','ok',$3)", sourceId, jobId,
                        static_cast<int>(result.items.size()));
                }
                std::cout << "{\"component\":\"native_collector\",\"event\":\"source_complete\",\"platform\":\""
                          << platform << "\",\"mode\":\"" << mode
                          << "\",\"items\":" << result.items.size() << "}\n";
                watchdog.heartbeat();
            }
        }
        std::cout << "{\"component\":\"native_collector\",\"event\":\"stopped\",\"platform\":\""
                  << platform << "\"}\n";
        curl_global_cleanup();
        return 0;
    }
    catch (const std::exception &)
    {
        std::cerr << "{\"component\":\"native_collector\",\"event\":\"fatal\",\"reason\":\"database_unavailable\",\"platform\":\""
                  << platform << "\"}\n";
        curl_global_cleanup();
        return 1;
    }
}
