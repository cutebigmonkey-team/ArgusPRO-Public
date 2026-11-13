#pragma once

#include <json/json.h>

#include <chrono>
#include <cstddef>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace argus::browser {

using StopPredicate = std::function<bool()>;

class CdpMessageRouter {
  public:
    int beginRequest();
    Json::Value waitForResponse(int id, std::chrono::milliseconds timeout,
                                const StopPredicate &stopRequested = {});
    bool ingest(std::string_view message);
    Json::Value waitForEvent(const std::string &method, std::chrono::milliseconds timeout,
                             const StopPredicate &stopRequested = {});
    std::vector<Json::Value> takeEvents(const std::string &method, std::size_t maximum);
    void failRequest(int id, const std::string &reason);
    void failAll(const std::string &reason);
    std::size_t malformedMessages() const;

  private:
    struct Pending;
    mutable std::mutex mutex_;
    int nextId_{1};
    std::unordered_map<int, std::shared_ptr<Pending>> pending_;
    std::vector<Json::Value> events_;
    std::size_t malformed_{0};
    static constexpr std::size_t maximumEvents_{2048};
};

class CdpClient {
  public:
    explicit CdpClient(std::string httpEndpoint = "http://argus-chromium:9222");
    ~CdpClient();
    CdpClient(const CdpClient &) = delete;
    CdpClient &operator=(const CdpClient &) = delete;

    Json::Value version(std::chrono::milliseconds timeout = std::chrono::seconds(5),
                        const StopPredicate &stopRequested = {}) const;
    Json::Value targets(std::chrono::milliseconds timeout = std::chrono::seconds(5),
                        const StopPredicate &stopRequested = {}) const;
    Json::Value createTargetViaHttp(const std::string &url,
                                    std::chrono::milliseconds timeout = std::chrono::seconds(5)) const;
    bool closeTargetViaHttp(const std::string &targetId,
                            std::chrono::milliseconds timeout = std::chrono::seconds(5)) const;

    void connectBrowser(std::chrono::milliseconds timeout = std::chrono::seconds(5),
                        const StopPredicate &stopRequested = {});
    void connectTarget(const std::string &targetId,
                       std::chrono::milliseconds timeout = std::chrono::seconds(5),
                       const StopPredicate &stopRequested = {});
    void connectWebSocket(const std::string &webSocketUrl,
                          std::chrono::milliseconds timeout = std::chrono::seconds(5),
                          const StopPredicate &stopRequested = {});
    void reconnect(std::chrono::milliseconds timeout = std::chrono::seconds(5),
                   const StopPredicate &stopRequested = {});
    bool connected() const;
    void stop();

    Json::Value command(const std::string &method,
                        const Json::Value &params = Json::Value(Json::objectValue),
                        std::chrono::milliseconds timeout = std::chrono::seconds(10),
                        const StopPredicate &stopRequested = {});
    Json::Value waitForEvent(const std::string &method,
                             std::chrono::milliseconds timeout = std::chrono::seconds(10),
                             const StopPredicate &stopRequested = {});
    std::vector<Json::Value> takeEvents(const std::string &method, std::size_t maximum = 100);

    const std::string &httpEndpoint() const { return httpEndpoint_; }
    CdpMessageRouter &routerForTests() { return *router_; }

  private:
    class Impl;
    std::string httpEndpoint_;
    std::string webSocketUrl_;
    std::shared_ptr<CdpMessageRouter> router_;
    std::unique_ptr<Impl> impl_;
};

}  // namespace argus::browser
