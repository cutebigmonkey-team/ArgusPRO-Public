#include "PipelineDb.h"

#include <drogon/drogon.h>
#include <iostream>

int main()
{
    using namespace argus::pipeline;
    std::cout << std::unitbuf;
    std::cerr << std::unitbuf;
    installSignalHandlers();
    const std::string worker = envString("ARGUS_WORKER_ID", "scheduler-vm");
    const int batch = envInt("ARGUS_SCHEDULER_BATCH_SIZE", 20, 1, 100);
    const int interval = envInt("ARGUS_SCHEDULER_INTERVAL_SECONDS", 5, 1, 300);
    const std::string platforms = envString("ARGUS_SCHEDULER_PLATFORMS", "rss");
    const bool shadow = envString("ARGUS_COLLECTOR_MODE", "shadow") != "writer";
    ProgressWatchdog watchdog(
        "scheduler", envInt("ARGUS_PROGRESS_TIMEOUT_SECONDS", 120, 30, 600));
    try
    {
        auto db = connectDatabase();
        std::cout << "{\"component\":\"scheduler\",\"event\":\"started\"}\n";
        while (!stopping())
        {
            watchdog.heartbeat();
            // One statement atomically claims only a bounded set of due sources,
            // advances their schedule, and enqueues one idempotent collection job.
            const auto result = shadow ? db->execSqlSync(
                "WITH due AS ("
                " SELECT ss.source_id,ss.next_check_at,ss.poll_interval_seconds,ss.priority"
                " FROM argus_shadow_source_state ss JOIN sources s ON s.id=ss.source_id"
                " JOIN platforms p ON p.id=s.platform_id WHERE s.enabled AND ss.next_check_at<=now()"
                " AND p.key=ANY(string_to_array($3,','))"
                " AND NOT EXISTS (SELECT 1 FROM argus_jobs active WHERE active.source_id=ss.source_id"
                " AND active.job_type='collect_source' AND active.status IN ('pending','retry','processing')"
                " AND active.payload->>'mode'='shadow')"
                " ORDER BY ss.priority DESC,ss.next_check_at"
                " FOR UPDATE OF ss SKIP LOCKED LIMIT $1::int),"
                " advanced AS (UPDATE argus_shadow_source_state ss SET claimed_at=now(),claimed_by=$2,"
                " next_check_at=now()+make_interval(secs=>due.poll_interval_seconds) FROM due"
                " WHERE ss.source_id=due.source_id RETURNING due.*)"
                " INSERT INTO argus_jobs(job_type,source_id,payload,priority,idempotency_key)"
                " SELECT 'collect_source',source_id,jsonb_build_object('mode','shadow','scheduled_at',next_check_at),priority,"
                " 'shadow:collect:'||source_id::text||':'||extract(epoch from next_check_at)::bigint::text FROM advanced"
                " ON CONFLICT DO NOTHING RETURNING id", batch, worker, platforms)
              : db->execSqlSync(
                "WITH due AS ("
                " SELECT ch.source_id, ch.next_check_at, ch.poll_interval_seconds, ch.priority"
                " FROM collector_health ch JOIN sources s ON s.id=ch.source_id JOIN platforms p ON p.id=s.platform_id"
                " WHERE s.enabled AND ch.next_check_at<=now()"
                " AND p.key=ANY(string_to_array($3,','))"
                " AND (ch.cooldown_until IS NULL OR ch.cooldown_until<=now())"
                " AND NOT EXISTS (SELECT 1 FROM argus_jobs active WHERE active.source_id=ch.source_id"
                " AND active.job_type='collect_source' AND active.status IN ('pending','retry','processing')"
                " AND COALESCE(active.payload->>'mode','writer')<>'shadow')"
                " ORDER BY ch.priority DESC, ch.next_check_at ASC"
                " FOR UPDATE OF ch SKIP LOCKED LIMIT $1::int),"
                " advanced AS (UPDATE collector_health ch SET claimed_at=now(), claimed_by=$2,"
                " next_check_at=now()+make_interval(secs=>due.poll_interval_seconds)"
                " FROM due WHERE ch.source_id=due.source_id RETURNING due.*)"
                " INSERT INTO argus_jobs(job_type,source_id,payload,priority,idempotency_key)"
                " SELECT 'collect_source',source_id,jsonb_build_object('scheduled_at',next_check_at),priority,"
                " 'collect:'||source_id::text||':'||extract(epoch from next_check_at)::bigint::text FROM advanced"
                " ON CONFLICT DO NOTHING RETURNING id", batch, worker, platforms);
            std::cout << "{\"component\":\"scheduler\",\"event\":\"batch\",\"jobs\":"
                      << result.size() << "}\n";
            watchdog.heartbeat();
            interruptibleWait(interval);
        }
        std::cout << "{\"component\":\"scheduler\",\"event\":\"stopped\"}\n";
        return 0;
    }
    catch (const std::exception &)
    {
        std::cerr << "{\"component\":\"scheduler\",\"event\":\"fatal\",\"reason\":\"database_unavailable\"}\n";
        return 1;
    }
}
