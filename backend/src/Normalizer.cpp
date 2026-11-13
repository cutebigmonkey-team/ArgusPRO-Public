#include "PipelineDb.h"

#include <drogon/drogon.h>
#include <json/json.h>
#include <iostream>
#include <sstream>

namespace {
std::string field(const Json::Value &v, const char *key) { return v.get(key, "").asString(); }
}

int main()
{
    using namespace argus::pipeline;
    std::cout << std::unitbuf;
    std::cerr << std::unitbuf;
    installSignalHandlers();
    const std::string worker = envString("ARGUS_WORKER_ID", "normalizer-vm");
    const int batch = envInt("ARGUS_NORMALIZER_BATCH_SIZE", 10, 1, 50);
    const bool shadow = envString("ARGUS_COLLECTOR_MODE", "shadow") != "writer";
    ProgressWatchdog watchdog(
        "normalizer", envInt("ARGUS_PROGRESS_TIMEOUT_SECONDS", 120, 30, 600));
    try
    {
        auto db = connectDatabase();
        db->execSqlSync(
            "UPDATE argus_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
            " available_at=now(),locked_at=NULL,locked_by=NULL,last_error='stale_normalizer_lock'"
            " WHERE job_type IN ('normalize_post','shadow_normalize_post') AND status='processing'"
            " AND (locked_by=$1 OR locked_at<now()-interval '15 minutes')",
            worker);
        std::cout << "{\"component\":\"normalizer\",\"event\":\"started\"}\n";
        while (!stopping())
        {
            watchdog.heartbeat();
            auto jobs = db->execSqlSync(
                "WITH picked AS (SELECT id FROM argus_jobs WHERE job_type=$3"
                " AND status IN ('pending','retry') AND available_at<=now()"
                " ORDER BY priority DESC,created_at FOR UPDATE SKIP LOCKED LIMIT $1::int)"
                " UPDATE argus_jobs j SET status='processing',locked_at=now(),locked_by=$2,attempts=attempts+1"
                " FROM picked WHERE j.id=picked.id RETURNING j.id::text,j.source_id::text,j.payload::text,j.attempts,j.max_attempts",
                batch, worker, shadow ? "shadow_normalize_post" : "normalize_post");
            if (jobs.empty()) { interruptibleWait(2); continue; }
            for (const auto &row : jobs)
            {
                if (stopping()) break;
                watchdog.heartbeat();
                const std::string jobId = row["id"].as<std::string>();
                try
                {
                    Json::Value post;
                    Json::CharReaderBuilder builder;
                    std::string errors;
                    std::istringstream input(row["payload"].as<std::string>());
                    if (!Json::parseFromStream(builder, input, &post, &errors)) throw std::runtime_error("invalid payload");
                    const std::string canonical = field(post, "canonical_key");
                    if (canonical.empty() || field(post, "post_url").empty()) throw std::runtime_error("missing canonical identity");
                    if (shadow)
                    {
                        db->execSqlSync(
                            "INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,observed_count,canonical_key,normalized_payload)"
                            " VALUES($1,$2,'normalize','ok',1,$3,$4::jsonb)",
                            row["source_id"].as<std::string>(), jobId, canonical, post.toStyledString());
                    }
                    else db->execSqlSync(
                        // Browser APIs commonly expose Unix epoch seconds (or
                        // milliseconds), while RSS/native adapters usually
                        // provide an ISO timestamp.  Normalize both forms in
                        // PostgreSQL so an otherwise valid post is not retried
                        // forever because a numeric epoch was cast directly to
                        // timestamptz.
                        "INSERT INTO posts(origin,platform_id,source_id,legacy_source_id,source_name,source_page_url,"
                        "post_url,content,text,published_at,sort_ts,dedup_key,canonical_key,idempotency_key,images,videos,mention_tone,region_id)"
                        " SELECT $1,s.platform_id,$2,s.legacy_id,s.name,s.url,$3,$4,$4,parsed.published_at,"
                        " (extract(epoch from COALESCE(parsed.published_at,now()))*1000)::bigint,$6,$6,$7,$8::jsonb,$9::jsonb,"
                        // Same DSNS-mention classification /api/posts and
                        // /api/analytics/summary read back — computed once
                        // here at insert time instead of per request (see
                        // migration 0040 for why). text==content in this
                        // insert path, so lower($4) alone covers both.
                        " CASE"
                        " WHEN lower($4) !~ '(дснс|гсчс|рятуваль|надзвичайн|пожежник|спасател|emergency service)' THEN ''"
                        " WHEN lower($4) ~ '(скарг|ганьб|бездіял|критик|обур|запізн|не приїх|коруп|поруш|провал|недбал|жалоб|бездейств|поздно|не приех|critic|failed)' THEN 'criticism'"
                        " WHEN lower($4) ~ '(дяку|подяк|молодц|геро|пишаєм|вдячн|спасиб|hero|thank)' THEN 'praise'"
                        " ELSE 'other' END,"
                        " s.region_id"
                        " FROM sources s CROSS JOIN LATERAL (SELECT CASE"
                        " WHEN NULLIF($5::text,'') IS NULL THEN NULL::timestamptz"
                        " WHEN $5::text ~ '^[0-9]{10}$' THEN to_timestamp($5::double precision)"
                        " WHEN $5::text ~ '^[0-9]{13}$' THEN to_timestamp($5::double precision / 1000.0)"
                        // PostgreSQL 17 can validate the native timestamptz
                        // input grammar without throwing. Invalid localized
                        // page labels must not poison/retry an otherwise valid
                        // post or block the normalizer queue.
                        " WHEN pg_input_is_valid($5::text,'timestamptz') THEN $5::timestamptz"
                        " ELSE NULL::timestamptz END AS candidate) raw_timestamp"
                        " CROSS JOIN LATERAL (SELECT CASE WHEN raw_timestamp.candidate"
                        " BETWEEN '2000-01-01'::timestamptz AND now()+interval '1 day'"
                        " THEN raw_timestamp.candidate ELSE NULL::timestamptz END AS published_at) parsed"
                        " WHERE s.id=$2"
                        " ON CONFLICT(dedup_key) DO UPDATE SET content=EXCLUDED.content,text=EXCLUDED.text,"
                        " published_at=COALESCE(EXCLUDED.published_at,posts.published_at),"
                        " sort_ts=CASE WHEN EXCLUDED.published_at IS NOT NULL THEN EXCLUDED.sort_ts"
                        " ELSE COALESCE(posts.sort_ts,"
                        " (extract(epoch from COALESCE(posts.published_at,now()))*1000)::bigint) END,"
                        " images=EXCLUDED.images,"
                        // region_id is re-applied on every re-collect (not just
                        // insert) so posts written during the 2026-07-23..08-05
                        // window this column was missing from the INSERT list
                        // self-heal the next time their source is re-crawled,
                        // without a separate backfill pass.
                        " videos=EXCLUDED.videos,collected_at=now(),mention_tone=EXCLUDED.mention_tone,"
                        " region_id=EXCLUDED.region_id",
                        field(post,"origin"), row["source_id"].as<std::string>(), field(post,"post_url"),
                        field(post,"text"), field(post,"published_at"), canonical, field(post,"idempotency_key"),
                        post.get("images", Json::arrayValue).toStyledString(), post.get("videos", Json::arrayValue).toStyledString());
                    if (stopping()) break;
                    db->execSqlSync("UPDATE argus_jobs SET status='completed',completed_at=now(),locked_at=NULL,locked_by=NULL WHERE id=$1", jobId);
                    if (stopping()) break;
                    if (!shadow)
                        db->execSqlSync("UPDATE collector_health SET last_status='ok',last_success_at=now(),last_checked_at=now(),consecutive_failures=0,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", row["source_id"].as<std::string>());
                    watchdog.heartbeat();
                }
                catch (const drogon::orm::TimeoutError &)
                {
                    // Do not issue a second synchronous write on a timed-out
                    // one-connection pool. Let the outer handler terminate;
                    // Compose restarts this worker and startup lease recovery
                    // immediately requeues jobs locked by the same worker ID.
                    throw;
                }
                catch (const std::exception &)
                {
                    db->execSqlSync(
                        "UPDATE argus_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
                        " available_at=now()+make_interval(secs=>LEAST(3600,30*(2^LEAST(attempts,7)))),"
                        " locked_at=NULL,locked_by=NULL,last_error='normalization_failed' WHERE id=$1", jobId);
                    if (shadow)
                    {
                        if (stopping()) break;
                        db->execSqlSync("INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,error_code) VALUES($1,$2,'normalize','failed','normalization_failed')", row["source_id"].as<std::string>(), jobId);
                    }
                    watchdog.heartbeat();
                }
            }
        }
        std::cout << "{\"component\":\"normalizer\",\"event\":\"stopped\"}\n";
        return 0;
    }
    catch (const std::exception &)
    {
        std::cerr << "{\"component\":\"normalizer\",\"event\":\"fatal\",\"reason\":\"database_unavailable\"}\n";
        return 1;
    }
}
