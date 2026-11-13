#pragma once

#include "CdpClient.h"

#include <chrono>
#include <cstddef>
#include <functional>
#include <string>
#include <vector>

namespace argus::browser {

class BrowserSession {
  public:
    explicit BrowserSession(std::string endpoint = "http://argus-chromium:9222");
    ~BrowserSession();
    BrowserSession(const BrowserSession &) = delete;
    BrowserSession &operator=(const BrowserSession &) = delete;

    void open(std::chrono::milliseconds timeout = std::chrono::seconds(10),
              const StopPredicate &stopRequested = {});
    void close() noexcept;
    bool opened() const { return !targetId_.empty(); }

    void setCookies(const Json::Value &cookies,
                    std::chrono::milliseconds timeout = std::chrono::seconds(5));
    void setExtraHeaders(const Json::Value &headers,
                         std::chrono::milliseconds timeout = std::chrono::seconds(5));
    void navigate(const std::string &url,
                  std::chrono::milliseconds timeout = std::chrono::seconds(30),
                  const StopPredicate &stopRequested = {});
    void stopLoading(std::chrono::milliseconds timeout = std::chrono::seconds(3));
    bool waitForSelector(const std::string &selector,
                         std::chrono::milliseconds timeout = std::chrono::seconds(10));
    bool waitForAnySelector(const std::vector<std::string> &selectors,
                            std::chrono::milliseconds timeout = std::chrono::seconds(10));
    std::vector<int> querySelectorAll(const std::string &selector,
                                      std::chrono::milliseconds timeout = std::chrono::seconds(5));
    std::string getOuterHtml(int nodeId,
                             std::chrono::milliseconds timeout = std::chrono::seconds(5));
    Json::Value evaluate(const std::string &expression,
                         std::chrono::milliseconds timeout = std::chrono::seconds(10));
    Json::Value getCookies(std::chrono::milliseconds timeout = std::chrono::seconds(5));
    Json::Value captureSnapshot(std::chrono::milliseconds timeout = std::chrono::seconds(10));
    void boundedScroll(int steps, std::chrono::milliseconds pause,
                       std::chrono::milliseconds commandTimeout = std::chrono::seconds(5),
                       const std::function<bool()> &stopRequested = {});
    std::vector<Json::Value> responseBodies(const std::vector<std::string> &urlNeedles,
                                            std::size_t maximum = 20,
                                            std::chrono::milliseconds timeout = std::chrono::seconds(5),
                                            const std::function<bool()> &stopRequested = {});
    void clearOriginData(const std::string &origin,
                         std::chrono::milliseconds timeout = std::chrono::seconds(5));

    static Json::Value loadCookiesFile(const std::string &path);
    const std::string &targetId() const { return targetId_; }
    const std::string &contextId() const { return contextId_; }

  private:
    std::string endpoint_;
    CdpClient browser_;
    CdpClient page_;
    std::string contextId_;
    std::string targetId_;
};

}  // namespace argus::browser
