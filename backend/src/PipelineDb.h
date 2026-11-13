#pragma once

#include <drogon/orm/DbClient.h>
#include <atomic>
#include <cstdint>
#include <memory>
#include <string>
#include <thread>

namespace argus::pipeline {

std::shared_ptr<drogon::orm::DbClient> connectDatabase();
void enqueueNormalizationJob(const std::shared_ptr<drogon::orm::DbClient> &db,
                             const std::string &sourceId,
                             const std::string &payload,
                             const std::string &idempotencyKey,
                             const std::string &jobType);
int envInt(const char *name, int fallback, int minimum, int maximum);
std::string envString(const char *name, const std::string &fallback = {});
void installSignalHandlers();
bool stopping();
void interruptibleWait(int seconds);

class ProgressWatchdog {
  public:
    ProgressWatchdog(std::string component, int timeoutSeconds);
    ~ProgressWatchdog();
    ProgressWatchdog(const ProgressWatchdog &) = delete;
    ProgressWatchdog &operator=(const ProgressWatchdog &) = delete;

    void heartbeat() noexcept;

  private:
    std::string component_;
    int timeoutSeconds_;
    std::atomic<std::int64_t> deadlineMilliseconds_;
    std::jthread thread_;
};

}  // namespace argus::pipeline
