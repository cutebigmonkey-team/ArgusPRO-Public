#include "PipelineDb.h"

#include <algorithm>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cstdio>
#include <csignal>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <thread>

namespace argus::pipeline {
namespace {
std::atomic_bool g_stopping{false};
void stopHandler(int) { g_stopping.store(true); }

std::string addParameter(std::string connection, const std::string &key, const std::string &value)
{
    if (connection.rfind("postgresql://", 0) == 0 || connection.rfind("postgres://", 0) == 0)
        return connection + (connection.find('?') == std::string::npos ? "?" : "&") + key + "=" + value;
    return connection + " " + key + "='" + value + "'";
}
}  // namespace

std::string envString(const char *name, const std::string &fallback)
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
    return fallback;
}

int envInt(const char *name, int fallback, int minimum, int maximum)
{
    const std::string raw = envString(name);
    int value = fallback;
    const auto result = std::from_chars(raw.data(), raw.data() + raw.size(), value);
    if (raw.empty() || result.ec != std::errc{} || result.ptr != raw.data() + raw.size()) value = fallback;
    return std::clamp(value, minimum, maximum);
}

std::shared_ptr<drogon::orm::DbClient> connectDatabase()
{
    std::string connection = envString("DATABASE_URL");
    const std::string sslmode = envString("DB_SSLMODE");
    const std::string ca = envString("SUPABASE_CA_CERT_PATH");
    if (connection.empty()) throw std::runtime_error("DATABASE_URL is not configured");
    if (sslmode != "verify-full") throw std::runtime_error("DB_SSLMODE must be verify-full");
    if (ca.empty() || !std::filesystem::is_regular_file(ca)) throw std::runtime_error("Supabase CA file is missing");
    connection = addParameter(std::move(connection), "sslmode", sslmode);
    connection = addParameter(std::move(connection), "sslrootcert", ca);
    connection = addParameter(std::move(connection), "connect_timeout",
                              std::to_string(envInt("DB_CONNECT_TIMEOUT", 10, 1, 60)));
    connection = addParameter(std::move(connection), "keepalives", "1");
    connection = addParameter(std::move(connection), "keepalives_idle",
                              std::to_string(envInt("DB_KEEPALIVES_IDLE_SECONDS", 30, 5, 300)));
    connection = addParameter(std::move(connection), "keepalives_interval",
                              std::to_string(envInt("DB_KEEPALIVES_INTERVAL_SECONDS", 10, 1, 60)));
    connection = addParameter(std::move(connection), "keepalives_count",
                              std::to_string(envInt("DB_KEEPALIVES_COUNT", 3, 1, 10)));
    connection = addParameter(std::move(connection), "tcp_user_timeout",
                              std::to_string(envInt("DB_TCP_USER_TIMEOUT_MS", 30000, 1000, 300000)));

    auto client = drogon::orm::DbClient::newPgClient(
        connection, static_cast<size_t>(envInt("DB_POOL_SIZE", 2, 1, 5)));
    client->setTimeout(static_cast<double>(envInt("DB_QUERY_TIMEOUT_SECONDS", 30, 1, 300)));
    return client;
}

void enqueueNormalizationJob(const std::shared_ptr<drogon::orm::DbClient> &db,
                             const std::string &sourceId,
                             const std::string &payload,
                             const std::string &idempotencyKey,
                             const std::string &jobType)
{
    db->execSqlSync(
        "INSERT INTO argus_jobs(job_type,source_id,payload,priority,idempotency_key)"
        " VALUES($4,$1,$2::jsonb,100,$3)"
        // A stable idempotency key still creates at most one durable job. If
        // a later collection observes a genuine payload change, recycle only
        // the already-finished job so edits/media/timestamp repairs reach the
        // posts upsert. Never overwrite pending or processing work, and never
        // move a canonical identity between sources or job modes.
        " ON CONFLICT(idempotency_key) DO UPDATE SET"
        " payload=EXCLUDED.payload,priority=GREATEST(argus_jobs.priority,EXCLUDED.priority),"
        " status='pending',available_at=now(),attempts=0,locked_at=NULL,locked_by=NULL,"
        " last_error=NULL,completed_at=NULL"
        " WHERE argus_jobs.source_id=EXCLUDED.source_id"
        " AND argus_jobs.job_type=EXCLUDED.job_type"
        " AND argus_jobs.status IN ('completed','dead_letter')"
        " AND argus_jobs.payload IS DISTINCT FROM EXCLUDED.payload",
        sourceId, payload, idempotencyKey, jobType);
}

void installSignalHandlers()
{
    std::signal(SIGINT, stopHandler);
    std::signal(SIGTERM, stopHandler);
}
bool stopping() { return g_stopping.load(); }
void interruptibleWait(int seconds)
{
    for (int i = 0; i < seconds * 10 && !stopping(); ++i)
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
}

namespace {
std::int64_t steadyMilliseconds() noexcept
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}
}  // namespace

ProgressWatchdog::ProgressWatchdog(std::string component, int timeoutSeconds)
    : component_(std::move(component)),
      timeoutSeconds_(std::clamp(timeoutSeconds, 30, 600)),
      deadlineMilliseconds_(steadyMilliseconds() +
                            static_cast<std::int64_t>(timeoutSeconds_) * 1000),
      thread_([this](std::stop_token stopToken) {
          while (!stopToken.stop_requested())
          {
              for (int step = 0; step < 10 && !stopToken.stop_requested(); ++step)
                  std::this_thread::sleep_for(std::chrono::milliseconds(100));
              if (stopToken.stop_requested() || stopping()) return;
              if (steadyMilliseconds() <= deadlineMilliseconds_.load()) continue;
              std::fprintf(stderr,
                           "{\"component\":\"%s\",\"event\":\"fatal\","
                           "\"reason\":\"progress_timeout\"}\n",
                           component_.c_str());
              std::fflush(stderr);
              std::_Exit(70);
          }
      })
{
}

ProgressWatchdog::~ProgressWatchdog()
{
    thread_.request_stop();
}

void ProgressWatchdog::heartbeat() noexcept
{
    deadlineMilliseconds_.store(
        steadyMilliseconds() + static_cast<std::int64_t>(timeoutSeconds_) * 1000);
}
}  // namespace argus::pipeline
