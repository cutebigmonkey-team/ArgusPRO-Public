#pragma once

#include <json/json.h>

#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace argus::browser {

struct BrowserItem {
    std::string canonicalKey;
    std::string postUrl;
    std::string text;
    std::string publishedAt;
    Json::Value images{Json::arrayValue};
    Json::Value videos{Json::arrayValue};
};

using Canonicalizer = std::function<std::pair<std::string, std::string>(const std::string &)>;
using SourceVerifier = std::function<bool(const Json::Value &, const std::string &)>;

struct AdapterSpec {
    std::string platform;
    std::string cookieEnvironment;
    std::vector<std::string> allowedHosts;
    std::vector<std::string> selectors;
    std::vector<std::string> networkUrlNeedles;
    int scrollSteps{5};
    int scrollPauseMs{1000};
    std::string extractionExpression;
    Canonicalizer canonicalizer;
    std::function<std::string(const std::string &)> navigationTransform;
    SourceVerifier sourceVerifier;
    std::vector<std::string> networkIdentityFields;
    std::vector<std::string> networkTimestampFields;
    std::string networkUrlTemplate;
    std::size_t fallbackThreshold{0};
    std::function<std::string(const std::string &)> fallbackNavigationTransform;
    std::vector<std::string> fallbackSelectors;
    std::string fallbackExtractionExpression;
    int fallbackScrollSteps{0};
    int fallbackScrollPauseMs{1000};
};

class PlatformAdapter {
  public:
    virtual ~PlatformAdapter() = default;
    virtual const AdapterSpec &spec() const = 0;
    virtual std::string navigationUrl(const std::string &sourceUrl) const;
    virtual std::string fallbackNavigationUrl(const std::string &sourceUrl) const;
    virtual bool needsFallback(std::size_t verifiedItems) const;
    virtual std::string classifyPage(const std::string &currentUrl,
                                     const std::string &bodyText) const;
    virtual std::string classifyEmptyPage(const std::string &currentUrl,
                                          const std::string &bodyText) const;
    virtual std::vector<BrowserItem> parseDom(const Json::Value &raw,
                                              std::size_t limit) const = 0;
    virtual std::vector<BrowserItem> parseDomForSource(const Json::Value &raw,
                                                       const std::string &sourceUrl,
                                                       std::size_t limit) const;
    virtual std::vector<BrowserItem> parseFallbackDom(const Json::Value &raw,
                                                      const std::string &sourceUrl,
                                                      std::size_t limit) const;
    virtual std::vector<BrowserItem> parseNetwork(const std::vector<Json::Value> &responses,
                                                  const std::string &sourceUrl,
                                                  std::size_t limit) const = 0;
};

std::unique_ptr<PlatformAdapter> makeDomAdapter(AdapterSpec spec);
std::unique_ptr<PlatformAdapter> makePlatformAdapter(const std::string &platform);

std::unique_ptr<PlatformAdapter> makeFacebookAdapter();
std::unique_ptr<PlatformAdapter> makeInstagramAdapter();
std::unique_ptr<PlatformAdapter> makeXAdapter();
std::unique_ptr<PlatformAdapter> makeThreadsAdapter();
std::unique_ptr<PlatformAdapter> makeTikTokAdapter();
std::unique_ptr<PlatformAdapter> makeVkAdapter();
std::unique_ptr<PlatformAdapter> makeOkAdapter();
std::unique_ptr<PlatformAdapter> makeMaxAdapter();
std::unique_ptr<PlatformAdapter> makeDsnsBrowserAdapter();

bool allowedHttpUrl(const std::string &url, const std::vector<std::string> &hosts);
std::string httpUrlPath(const std::string &url);
std::string stripFragment(const std::string &url);
std::string stripQueryAndFragment(const std::string &url);
std::string regexCapture(const std::string &value, const std::string &pattern,
                         std::size_t group = 1);

}  // namespace argus::browser
