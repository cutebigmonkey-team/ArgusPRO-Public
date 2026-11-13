#include "PipelineDb.h"
#include "browser/BrowserPool.h"
#include "browser/ThreadsCircuitBreaker.h"
#include "platforms/PlatformAdapter.h"

#include <drogon/drogon.h>
#include <json/json.h>
#include <curl/curl.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <vector>

namespace {

using argus::browser::BrowserItem;
using argus::browser::PlatformAdapter;

struct StopRequested {};

void ensureRunning()
{
    if (argus::pipeline::stopping()) throw StopRequested{};
}

struct PlatformRuntime {
    std::string name;
    std::unique_ptr<PlatformAdapter> adapter;
    std::optional<argus::browser::ThreadsCircuitBreaker> threadsCircuitBreaker;
    bool cooldownWasOpen{false};
};

std::vector<std::size_t> smoothWeightedSchedule(const std::vector<int> &weights)
{
    std::vector<std::size_t> schedule;
    if (weights.empty()) return schedule;
    int total = 0;
    for (const int weight : weights) total += std::max(1, weight);
    std::vector<int> current(weights.size(), 0);
    schedule.reserve(static_cast<std::size_t>(total));
    for (int slot = 0; slot < total; ++slot)
    {
        std::size_t selected = 0;
        for (std::size_t index = 0; index < weights.size(); ++index)
        {
            current[index] += std::max(1, weights[index]);
            if (current[index] > current[selected]) selected = index;
        }
        current[selected] -= total;
        schedule.push_back(selected);
    }
    return schedule;
}

std::string trim(std::string value)
{
    const auto notSpace = [](unsigned char character) { return !std::isspace(character); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), notSpace));
    value.erase(std::find_if(value.rbegin(), value.rend(), notSpace).base(), value.end());
    return value;
}

std::vector<std::string> parsePlatforms(const std::string &configured)
{
    std::vector<std::string> platforms;
    std::set<std::string> seen;
    std::size_t begin = 0;
    while (begin <= configured.size())
    {
        const std::size_t comma = configured.find(',', begin);
        std::string platform = trim(configured.substr(
            begin, comma == std::string::npos ? std::string::npos : comma - begin));
        if (platform.empty()) return {};
        if (seen.insert(platform).second) platforms.push_back(std::move(platform));
        if (comma == std::string::npos) break;
        begin = comma + 1;
    }
    return platforms;
}

std::shared_ptr<drogon::orm::DbClient> connectWithBackoff()
{
    using namespace argus::pipeline;
    const int attempts = envInt("ARGUS_DB_CONNECT_ATTEMPTS", 6, 1, 10);
    int delay = 1;
    for (int attempt = 1; attempt <= attempts && !stopping(); ++attempt)
    {
        try { return connectDatabase(); }
        catch (...)
        {
            std::cerr << "{\"component\":\"browser_collector\",\"event\":\"db_connect_retry\",\"attempt\":"
                      << attempt << "}\n";
            if (attempt < attempts) interruptibleWait(delay);
            delay = std::min(delay * 2, 30);
        }
    }
    throw std::runtime_error("database_unavailable");
}

std::string cookiePath(const PlatformAdapter &adapter)
{
    using argus::pipeline::envString;
    const auto &spec = adapter.spec();
    if (spec.cookieEnvironment.empty()) return {};
    const std::string configured = envString(spec.cookieEnvironment.c_str());
    if (!configured.empty()) return configured;
    return "/run/secrets/collector-cookies/" + spec.platform + ".json";
}

void loadCookiesWhenPresent(argus::browser::BrowserSession &session,
                            const PlatformAdapter &adapter)
{
    if (adapter.spec().cookieEnvironment.empty()) return;
    const std::string path = cookiePath(adapter);
    std::error_code error;
    const bool exists = std::filesystem::exists(path, error);
    if (error) throw std::runtime_error("auth_required");
    if (!exists) return;
    if (!std::filesystem::is_regular_file(path, error) || error)
        throw std::runtime_error("auth_required");
    // BrowserSession parses this file through a read-only stream. Invalid or
    // empty existing files remain an authentication failure; only a genuinely
    // absent file selects anonymous collection.
    session.setCookies(argus::browser::BrowserSession::loadCookiesFile(path));
}

Json::Value payloadFor(const BrowserItem &item, const std::string &origin,
                       const std::string &platform)
{
    Json::Value payload;
    payload["origin"] = origin;
    payload["platform"] = platform;
    payload["post_url"] = item.postUrl;
    payload["text"] = item.text;
    payload["published_at"] = item.publishedAt;
    payload["canonical_key"] = item.canonicalKey;
    payload["idempotency_key"] = item.canonicalKey;
    payload["images"] = item.images;
    payload["videos"] = item.videos;
    return payload;
}

std::string safeError(const std::exception &error);

std::vector<BrowserItem> collect(PlatformAdapter &adapter, const std::string &sourceUrl,
                                 std::size_t maximum, argus::browser::BrowserPool &pool,
                                 argus::pipeline::ProgressWatchdog &watchdog)
{
    using namespace std::chrono_literals;
    ensureRunning();
    if (!argus::browser::allowedHttpUrl(sourceUrl, adapter.spec().allowedHosts))
        throw std::runtime_error("source_url_not_allowed");
    const std::string navigationUrl = adapter.navigationUrl(sourceUrl);
    if (navigationUrl.empty() ||
        !argus::browser::allowedHttpUrl(navigationUrl, adapter.spec().allowedHosts))
        throw std::runtime_error("source_url_not_allowed");
    const int acquireSeconds = argus::pipeline::envInt("ARGUS_BROWSER_ACQUIRE_TIMEOUT_SECONDS", 10, 2, 30);
    watchdog.heartbeat();
    auto session = pool.acquire(
        std::chrono::seconds(acquireSeconds),
        [] { return argus::pipeline::stopping(); });
    watchdog.heartbeat();
    ensureRunning();
    loadCookiesWhenPresent(*session, adapter);
    Json::Value headers;
    headers["Accept-Language"] = "uk-UA,uk;q=0.9,en;q=0.7";
    session->setExtraHeaders(headers);

    const int navigationSeconds = argus::pipeline::envInt("ARGUS_BROWSER_NAV_TIMEOUT_SECONDS", 30, 5, 60);
    const auto navigateTo = [&](const std::string &target) {
        bool navigated = false;
        for (int attempt = 1; attempt <= 2 && !argus::pipeline::stopping(); ++attempt)
        {
            watchdog.heartbeat();
            try
            {
                session->navigate(
                    target, std::chrono::seconds(navigationSeconds),
                    [] { return argus::pipeline::stopping(); });
                navigated = true;
                break;
            }
            catch (const std::runtime_error &error)
            {
                if (attempt == 2 || std::string(error.what()) != "navigation_timeout") throw;
                argus::pipeline::interruptibleWait(attempt);
            }
        }
        if (!navigated)
        {
            ensureRunning();
            throw std::runtime_error("navigation_timeout");
        }
        ensureRunning();
    };
    navigateTo(navigationUrl);
    ensureRunning();

    constexpr const char *pageProbe =
        "(()=>({url:location.href,text:(document.body&&document.body.innerText)||'',"
        "tiktokWaf:!!document.querySelector('#slardar-config,._wafchallengeid,.waforiginalreid')}))()";
    const Json::Value pageState = session->evaluate(pageProbe, 5s);
    watchdog.heartbeat();
    if (!argus::browser::allowedHttpUrl(pageState.get("url", "").asString(),
                                        adapter.spec().allowedHosts))
        throw std::runtime_error("redirect_not_allowed");
    if (const std::string classification = adapter.classifyPage(
            pageState.get("url", "").asString(), pageState.get("text", "").asString());
        !classification.empty()) throw std::runtime_error(classification);
    if (adapter.spec().platform == "tiktok" && pageState.get("tiktokWaf", false).asBool())
        throw std::runtime_error("challenge_required");

    const int scrollSteps = argus::pipeline::envInt(
        "ARGUS_BROWSER_SCROLL_STEPS", adapter.spec().scrollSteps, 1,
        std::max(1, adapter.spec().scrollSteps));
    const int scrollPauseMs = argus::pipeline::envInt(
        "ARGUS_BROWSER_SCROLL_PAUSE_MS", adapter.spec().scrollPauseMs, 100, 5000);
    session->boundedScroll(scrollSteps, std::chrono::milliseconds(scrollPauseMs), 5s,
                           [] { return argus::pipeline::stopping(); });
    watchdog.heartbeat();
    ensureRunning();
    (void)session->waitForAnySelector(adapter.spec().selectors, 8s);
    watchdog.heartbeat();
    const auto network = session->responseBodies(
        adapter.spec().networkUrlNeedles, 20, 5s,
        [] { return argus::pipeline::stopping(); });
    watchdog.heartbeat();
    ensureRunning();
    std::vector<BrowserItem> networkItems = adapter.parseNetwork(network, sourceUrl, maximum);
    const Json::Value raw = session->evaluate(adapter.spec().extractionExpression, 10s);
    watchdog.heartbeat();
    auto domItems = adapter.parseDomForSource(raw, sourceUrl, maximum);

    std::map<std::string, std::size_t> seen;
    std::vector<BrowserItem> merged;
    for (auto &item : networkItems)
        if (seen.emplace(item.canonicalKey, merged.size()).second) merged.push_back(std::move(item));
    for (auto &item : domItems)
    {
        // Network payloads (e.g. Instagram's GraphQL caption edges) often
        // omit the post text even though the identity/timestamp fields are
        // present, so a network item can win the merge with an empty
        // caption. The DOM scrape reliably captures the rendered caption, so
        // backfill it onto the already-merged network item instead of
        // discarding it as a duplicate.
        if (const auto existing = seen.find(item.canonicalKey); existing != seen.end())
        {
            if (merged[existing->second].text.empty()) merged[existing->second].text = item.text;
            continue;
        }
        if (merged.size() < maximum)
        {
            seen.emplace(item.canonicalKey, merged.size());
            merged.push_back(std::move(item));
        }
    }
    if (adapter.needsFallback(merged.size()) && merged.size() < maximum)
    {
        const std::size_t desktopItems = merged.size();
        try
        {
            const std::string fallbackUrl = adapter.fallbackNavigationUrl(sourceUrl);
            if (fallbackUrl.empty() ||
                !argus::browser::allowedHttpUrl(fallbackUrl, adapter.spec().allowedHosts))
                throw std::runtime_error("source_url_not_allowed");
            navigateTo(fallbackUrl);
            const Json::Value fallbackPageState = session->evaluate(pageProbe, 5s);
            watchdog.heartbeat();
            ensureRunning();
            const std::string fallbackCurrentUrl = fallbackPageState.get("url", "").asString();
            if (!argus::browser::allowedHttpUrl(fallbackCurrentUrl, adapter.spec().allowedHosts))
                throw std::runtime_error("redirect_not_allowed");
            if (const std::string classification = adapter.classifyPage(
                    fallbackCurrentUrl, fallbackPageState.get("text", "").asString());
                !classification.empty()) throw std::runtime_error(classification);

            if (adapter.spec().fallbackScrollSteps > 0)
            {
                session->boundedScroll(
                    adapter.spec().fallbackScrollSteps,
                    std::chrono::milliseconds(adapter.spec().fallbackScrollPauseMs), 5s,
                    [] { return argus::pipeline::stopping(); });
                watchdog.heartbeat();
                ensureRunning();
            }
            (void)session->waitForAnySelector(adapter.spec().fallbackSelectors, 8s);
            watchdog.heartbeat();
            const Json::Value fallbackRaw = session->evaluate(
                adapter.spec().fallbackExtractionExpression, 10s);
            watchdog.heartbeat();
            ensureRunning();
            auto fallbackItems = adapter.parseFallbackDom(fallbackRaw, sourceUrl, maximum);
            std::size_t acceptedFallbackItems = 0;
            for (auto &item : fallbackItems)
            {
                if (merged.size() >= maximum) break;
                if (seen.emplace(item.canonicalKey, merged.size()).second)
                {
                    merged.push_back(std::move(item));
                    ++acceptedFallbackItems;
                }
            }
            std::cout << "{\"component\":\"browser_collector\","
                      << "\"event\":\"facebook_mbasic_fallback\","
                      << "\"desktop_items\":" << desktopItems << ','
                      << "\"fallback_items\":" << acceptedFallbackItems << "}\n";
        }
        catch (const std::exception &error)
        {
            if (desktopItems == 0) throw;
            std::cout << "{\"component\":\"browser_collector\","
                      << "\"event\":\"facebook_mbasic_fallback_failed\","
                      << "\"error_code\":\"" << safeError(error) << "\","
                      << "\"retained_desktop_items\":" << desktopItems << "}\n";
        }
    }
    if (merged.empty())
    {
        if (adapter.spec().platform == "threads" || adapter.spec().platform == "tiktok")
        {
            Json::Value emptyPageState = pageState;
            try
            {
                emptyPageState = session->evaluate(pageProbe, 5s);
                watchdog.heartbeat();
            }
            catch (...) {}
            const std::string currentUrl = emptyPageState.get("url", "").asString();
            if (!argus::browser::allowedHttpUrl(currentUrl, adapter.spec().allowedHosts))
                throw std::runtime_error("redirect_not_allowed");
            if (adapter.spec().platform == "tiktok" &&
                emptyPageState.get("tiktokWaf", false).asBool())
                throw std::runtime_error("challenge_required");
            if (const std::string classification = adapter.classifyEmptyPage(
                    currentUrl, emptyPageState.get("text", "").asString());
                !classification.empty()) throw std::runtime_error(classification);
        }
        throw std::runtime_error("selector_regression");
    }
    return merged;
}

std::string safeError(const std::exception &error)
{
    static const std::set<std::string> allowed{
        "auth_required", "challenge_required", "rate_limited", "navigation_timeout",
        "selector_not_found", "selector_regression", "empty_shell", "source_url_not_allowed",
        "redirect_not_allowed", "invalid_payload", "browser_unavailable", "collection_failed"};
    const std::string value = error.what();
    if (value == "cdp_timeout") return "navigation_timeout";
    if (value == "cdp_disconnected" || value == "cdp_command_failed") return "browser_unavailable";
    return allowed.contains(value) ? value : "collection_failed";
}

std::size_t recoverStaleJobs(const std::shared_ptr<drogon::orm::DbClient> &db,
                             const std::string &platform, bool writer,
                             const std::string &worker)
{
    const auto recovered = db->execSqlSync(
        "UPDATE argus_jobs j SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
        " available_at=now(),locked_at=NULL,locked_by=NULL,last_error='stale_collector_lock'"
        " FROM sources s JOIN platforms p ON p.id=s.platform_id"
        " WHERE j.source_id=s.id AND p.key=$1 AND j.job_type='collect_source'"
        " AND (($2 AND COALESCE(j.payload->>'mode','writer')='writer')"
        " OR (NOT $2 AND j.payload->>'mode'='shadow')) AND j.status='processing'"
        " AND (j.locked_by=$3 OR j.locked_at<now()-interval '15 minutes')"
        " RETURNING j.id", platform, writer, worker);
    return recovered.size();
}

void failJob(const std::shared_ptr<drogon::orm::DbClient> &db,
             const std::string &jobId, const std::string &sourceId,
             const std::string &platform, const std::string &error,
             bool writer)
{
    db->execSqlSync(
        "UPDATE argus_jobs SET status=CASE WHEN attempts>=max_attempts THEN 'dead_letter' ELSE 'retry' END,"
        " available_at=now()+make_interval(secs=>LEAST(3600,30*(2^LEAST(attempts,7)))),"
        " locked_at=NULL,locked_by=NULL,last_error=$2 WHERE id=$1", jobId, error);
    if (argus::pipeline::stopping()) return;
    if (writer)
        db->execSqlSync(
            "UPDATE collector_health SET last_status='failed',last_checked_at=now(),"
            " consecutive_failures=consecutive_failures+1,claimed_at=NULL,claimed_by=NULL"
            " WHERE source_id=$1", sourceId);
    else
    {
        db->execSqlSync(
            "UPDATE argus_shadow_source_state SET last_status='failed',last_checked_at=now(),last_error=$2,"
            " claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", sourceId, error);
        if (argus::pipeline::stopping()) return;
        db->execSqlSync(
            "INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,error_code)"
            " VALUES($1,$2,'collect','failed',$3)", sourceId, jobId, error);
    }
    std::cout << "{\"component\":\"browser_collector\",\"event\":\"source_failed\",\"platform\":\""
              << platform << "\",\"mode\":\"" << (writer ? "writer" : "shadow")
              << "\",\"error_code\":\"" << error << "\"}\n";
}

}  // namespace

int main()
{
    using namespace argus::pipeline;
    std::cout << std::unitbuf;
    std::cerr << std::unitbuf;
    installSignalHandlers();
    const std::string mode = envString("ARGUS_COLLECTOR_MODE", "shadow");
    const bool writer = mode == "writer";
    if (mode != "shadow" && !writer)
    {
        std::cerr << "{\"component\":\"browser_collector\",\"event\":\"fatal\",\"reason\":\"invalid_collector_mode\"}\n";
        return 2;
    }

    const std::string multiplexConfiguration = envString("ARGUS_PLATFORMS");
    const bool multiplex = !multiplexConfiguration.empty();
    std::vector<std::string> platformNames = multiplex
        ? parsePlatforms(multiplexConfiguration)
        : std::vector<std::string>{envString("ARGUS_PLATFORM")};
    if (platformNames.empty() || platformNames.front().empty())
    {
        std::cerr << "{\"component\":\"browser_collector\",\"event\":\"fatal\",\"reason\":\"invalid_platform_configuration\"}\n";
        return 2;
    }

    const int threadsFailureThreshold = envInt("ARGUS_THREADS_COOLDOWN_THRESHOLD", 3, 2, 10);
    const int threadsCooldownSeconds = envInt("ARGUS_THREADS_COOLDOWN_SECONDS", 600, 60, 1800);
    std::vector<PlatformRuntime> platforms;
    platforms.reserve(platformNames.size());
    for (auto &name : platformNames)
    {
        auto adapter = argus::browser::makePlatformAdapter(name);
        if (!adapter)
        {
            std::cerr << "{\"component\":\"browser_collector\",\"event\":\"fatal\",\"reason\":\"unsupported_platform\"}\n";
            return 2;
        }
        std::optional<argus::browser::ThreadsCircuitBreaker> breaker;
        if (name == "threads")
            breaker.emplace(static_cast<std::size_t>(threadsFailureThreshold),
                            std::chrono::seconds(threadsCooldownSeconds));
        platforms.push_back(PlatformRuntime{
            std::move(name), std::move(adapter), std::move(breaker), false});
    }
    curl_global_init(CURL_GLOBAL_DEFAULT);

    const std::string scope = multiplex ? "multiplex" : platforms.front().name;
    const std::string worker = envString(
        "ARGUS_WORKER_ID", scope + (writer ? "-writer-vm" : "-shadow-vm"));
    const int maximum = envInt("ARGUS_COLLECTOR_MAX_ITEMS", 20, 1, 50);
    const int idle = envInt("ARGUS_COLLECTOR_IDLE_SECONDS", 3, 1, 30);
    const int staleSweepSeconds = envInt("ARGUS_STALE_LOCK_SWEEP_SECONDS", 60, 30, 300);
    ProgressWatchdog watchdog(
        "browser_collector", envInt("ARGUS_PROGRESS_TIMEOUT_SECONDS", 180, 60, 600));
    argus::browser::BrowserPool pool(envString("ARGUS_CDP_ENDPOINT", "http://argus-chromium:9222"), 1);
    try
    {
        auto db = connectWithBackoff();
        std::vector<int> platformWeights(platforms.size(), 1);
        if (multiplex)
        {
            const auto counts = db->execSqlSync(
                "SELECT p.key,count(*)::int AS sources FROM sources s"
                " JOIN platforms p ON p.id=s.platform_id"
                " WHERE s.enabled AND p.key=ANY(string_to_array($1,',')) GROUP BY p.key",
                multiplexConfiguration);
            for (const auto &row : counts)
            {
                const std::string name = row["key"].as<std::string>();
                const int sources = row["sources"].as<int>();
                const auto runtime = std::find_if(
                    platforms.begin(), platforms.end(),
                    [&name](const PlatformRuntime &candidate) { return candidate.name == name; });
                if (runtime != platforms.end())
                    platformWeights[static_cast<std::size_t>(runtime - platforms.begin())] =
                        std::clamp((sources + 24) / 25, 1, 16);
            }
        }
        const std::vector<std::size_t> platformSchedule =
            smoothWeightedSchedule(platformWeights);
        std::size_t initiallyRecovered = 0;
        for (const auto &runtime : platforms)
        {
            watchdog.heartbeat();
            initiallyRecovered += recoverStaleJobs(db, runtime.name, writer, worker);
        }
        auto nextStaleSweep = std::chrono::steady_clock::now() +
                              std::chrono::seconds(staleSweepSeconds);
        std::cout << "{\"component\":\"browser_collector\",\"event\":\"started\",\"platform\":\""
                  << scope << "\",\"mode\":\"" << mode << "\",\"concurrency\":1,\"platform_count\":"
                  << platforms.size() << ",\"schedule_slots\":" << platformSchedule.size()
                  << ",\"stale_recovered\":" << initiallyRecovered << "}\n";
        std::size_t nextPlatform = 0;
        while (!stopping())
        {
            watchdog.heartbeat();
            if (std::chrono::steady_clock::now() >= nextStaleSweep)
            {
                for (const auto &runtime : platforms)
                {
                    watchdog.heartbeat();
                    const auto recovered = recoverStaleJobs(db, runtime.name, writer, worker);
                    if (recovered > 0)
                        std::cout << "{\"component\":\"browser_collector\",\"event\":\"stale_jobs_recovered\",\"platform\":\""
                                  << runtime.name << "\",\"mode\":\"" << mode
                                  << "\",\"jobs\":" << recovered << "}\n";
                }
                nextStaleSweep = std::chrono::steady_clock::now() +
                                 std::chrono::seconds(staleSweepSeconds);
            }

            bool handledJob = false;
            for (std::size_t inspected = 0;
                 inspected < platformSchedule.size() && !stopping(); ++inspected)
            {
                PlatformRuntime &runtime = platforms[platformSchedule[nextPlatform]];
                nextPlatform = (nextPlatform + 1) % platformSchedule.size();
                if (runtime.threadsCircuitBreaker)
                {
                    const auto now = argus::browser::ThreadsCircuitBreaker::Clock::now();
                    if (runtime.threadsCircuitBreaker->isOpen(now))
                    {
                        runtime.cooldownWasOpen = true;
                        continue;
                    }
                    if (runtime.cooldownWasOpen)
                    {
                        runtime.cooldownWasOpen = false;
                        std::cout << "{\"component\":\"browser_collector\",\"event\":\"platform_cooldown_ended\","
                                  << "\"platform\":\"threads\",\"mode\":\"" << mode << "\"}\n";
                    }
                }
                watchdog.heartbeat();
                const auto jobs = db->execSqlSync(
                    "WITH picked AS (SELECT j.id,j.source_id,s.origin,s.url FROM argus_jobs j"
                    " JOIN sources s ON s.id=j.source_id JOIN platforms p ON p.id=s.platform_id"
                    " LEFT JOIN collector_health ch ON ch.source_id=s.id"
                    " LEFT JOIN argus_shadow_source_state ss ON ss.source_id=s.id"
                    " WHERE j.job_type='collect_source' AND p.key=$2 AND j.status IN ('pending','retry')"
                    " AND (($3 AND COALESCE(j.payload->>'mode','writer')='writer')"
                    " OR (NOT $3 AND j.payload->>'mode'='shadow')) AND j.available_at<=now()"
                    // Within each platform, origin='dsns' sources are always
                    // claimed ahead of general OSINT ones — the single shared
                    // Chromium session (concurrency 1) cannot keep every
                    // enabled source within its poll_interval, and this
                    // product exists to monitor DSNS, so that capacity should
                    // go to DSNS sources first. Among sources of the same
                    // origin, the least-recently checked one wins; priority
                    // remains the final tie-breaker so repeated high-priority
                    // auth/selector failures cannot starve the rest of a
                    // platform forever.
                    " ORDER BY (s.origin='dsns') DESC,"
                    " CASE WHEN $3 THEN ch.last_checked_at ELSE ss.last_checked_at END"
                    " ASC NULLS FIRST,j.priority DESC,j.created_at"
                    " FOR UPDATE OF j SKIP LOCKED LIMIT 1)"
                    " UPDATE argus_jobs j SET status='processing',locked_at=now(),locked_by=$1,attempts=attempts+1"
                    " FROM picked WHERE j.id=picked.id"
                    " RETURNING j.id::text,picked.source_id::text,picked.origin,picked.url",
                    worker, runtime.name, writer);
                if (jobs.empty()) continue;
                handledJob = true;
                for (const auto &row : jobs)
                {
                    watchdog.heartbeat();
                    const std::string jobId = row["id"].as<std::string>();
                    const std::string sourceId = row["source_id"].as<std::string>();
                    try
                    {
                        const auto items = collect(*runtime.adapter, row["url"].as<std::string>(),
                                                   maximum, pool, watchdog);
                        ensureRunning();
                        if (runtime.threadsCircuitBreaker)
                        {
                            runtime.threadsCircuitBreaker->recordSuccess();
                            runtime.cooldownWasOpen = false;
                        }
                        for (const auto &item : items)
                        {
                            ensureRunning();
                            watchdog.heartbeat();
                            const Json::Value payload = payloadFor(
                                item, row["origin"].as<std::string>(), runtime.name);
                            enqueueNormalizationJob(
                                db,
                                sourceId, payload.toStyledString(),
                                (writer ? "normalize:" : "shadow:normalize:") + item.canonicalKey,
                                writer ? "normalize_post" : "shadow_normalize_post");
                        }
                        ensureRunning();
                        db->execSqlSync(
                            "UPDATE argus_jobs SET status='completed',completed_at=now(),locked_at=NULL,locked_by=NULL"
                            " WHERE id=$1", jobId);
                        ensureRunning();
                        if (writer)
                            db->execSqlSync(
                                "UPDATE collector_health SET last_status='ok',last_checked_at=now(),last_success_at=now(),"
                                " consecutive_failures=0,claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", sourceId);
                        else
                        {
                            db->execSqlSync(
                                "UPDATE argus_shadow_source_state SET last_status='ok',last_checked_at=now(),last_error=NULL,"
                                " claimed_at=NULL,claimed_by=NULL WHERE source_id=$1", sourceId);
                            ensureRunning();
                            db->execSqlSync(
                                "INSERT INTO argus_shadow_reports(source_id,job_id,stage,status,observed_count)"
                                " VALUES($1,$2,'collect','ok',$3)", sourceId, jobId,
                                static_cast<int>(items.size()));
                        }
                        std::cout << "{\"component\":\"browser_collector\",\"event\":\"source_complete\",\"platform\":\""
                                  << runtime.name << "\",\"mode\":\"" << mode
                                  << "\",\"items\":" << items.size() << "}\n";
                        watchdog.heartbeat();
                    }
                    catch (const drogon::orm::TimeoutError &)
                    {
                        throw;
                    }
                    catch (const StopRequested &)
                    {
                        break;
                    }
                    catch (const std::exception &error)
                    {
                        if (stopping()) break;
                        const std::string errorCode = safeError(error);
                        if (runtime.threadsCircuitBreaker)
                        {
                            if (argus::browser::ThreadsCircuitBreaker::observes(errorCode))
                            {
                                if (runtime.threadsCircuitBreaker->recordUnavailable())
                                {
                                    runtime.cooldownWasOpen = true;
                                    std::cout << "{\"component\":\"browser_collector\","
                                              << "\"event\":\"platform_cooldown_started\","
                                              << "\"platform\":\"threads\",\"mode\":\"" << mode
                                              << "\",\"reason\":\"" << errorCode
                                              << "\",\"threshold\":"
                                              << runtime.threadsCircuitBreaker->threshold()
                                              << ",\"duration_seconds\":"
                                              << runtime.threadsCircuitBreaker->cooldown().count() << "}\n";
                                }
                            }
                            else runtime.threadsCircuitBreaker->resetStreak();
                        }
                        failJob(db, jobId, sourceId, runtime.name, errorCode, writer);
                        watchdog.heartbeat();
                    }
                }
                break;
            }
            if (!handledJob)
                interruptibleWait(idle);
        }
        std::cout << "{\"component\":\"browser_collector\",\"event\":\"stopped\",\"platform\":\""
                  << scope << "\"}\n";
        curl_global_cleanup();
        return 0;
    }
    catch (...)
    {
        std::cerr << "{\"component\":\"browser_collector\",\"event\":\"fatal\",\"reason\":\"database_unavailable\",\"platform\":\""
                  << scope << "\"}\n";
        curl_global_cleanup();
        return 1;
    }
}
