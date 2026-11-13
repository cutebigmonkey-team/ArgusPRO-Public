#include "BrowserSession.h"

#include <algorithm>
#include <cctype>
#include <fstream>
#include <stdexcept>
#include <thread>

namespace argus::browser {
namespace {

constexpr std::size_t maximumResponseBodyBytes = 4 * 1024 * 1024;
constexpr std::size_t maximumTotalResponseBodyBytes = 16 * 1024 * 1024;

Json::Value paramsWithTimeout(const std::string &expression)
{
    Json::Value params;
    params["expression"] = expression;
    params["returnByValue"] = true;
    params["awaitPromise"] = true;
    return params;
}

bool containsAny(const std::string &value, const std::vector<std::string> &needles)
{
    return std::any_of(needles.begin(), needles.end(), [&](const std::string &needle) {
        return !needle.empty() && value.find(needle) != std::string::npos;
    });
}

bool stopWasRequested(const StopPredicate &stopRequested) noexcept
{
    if (!stopRequested) return false;
    try
    {
        return stopRequested();
    }
    catch (...)
    {
        return true;
    }
}

void throwIfStopRequested(const StopPredicate &stopRequested)
{
    if (stopWasRequested(stopRequested)) throw std::runtime_error("cdp_cancelled");
}

}  // namespace

BrowserSession::BrowserSession(std::string endpoint)
    : endpoint_(std::move(endpoint)), browser_(endpoint_), page_(endpoint_)
{
}

BrowserSession::~BrowserSession() { close(); }

void BrowserSession::open(std::chrono::milliseconds timeout,
                          const StopPredicate &stopRequested)
{
    throwIfStopRequested(stopRequested);
    if (opened()) return;
    browser_.connectBrowser(timeout, stopRequested);
    throwIfStopRequested(stopRequested);
    // Exercise the Browser domain on the browser-level WebSocket and fail
    // early if the endpoint is not a compatible Chromium CDP service.
    (void)browser_.command(
        "Browser.getVersion", Json::Value(Json::objectValue), timeout, stopRequested);
    throwIfStopRequested(stopRequested);
    Json::Value contextParams;
    contextParams["disposeOnDetach"] = true;
    contextId_ = browser_.command(
                             "Target.createBrowserContext", contextParams, timeout, stopRequested)
                     .get("browserContextId", "").asString();
    if (contextId_.empty()) throw std::runtime_error("browser_unavailable");
    throwIfStopRequested(stopRequested);
    Json::Value targetParams;
    targetParams["url"] = "about:blank";
    targetParams["browserContextId"] = contextId_;
    targetParams["background"] = false;
    targetId_ = browser_.command(
                            "Target.createTarget", targetParams, timeout, stopRequested)
                    .get("targetId", "").asString();
    if (targetId_.empty())
    {
        close();
        throw std::runtime_error("browser_unavailable");
    }
    for (int attempt = 0; attempt < 20; ++attempt)
    {
        throwIfStopRequested(stopRequested);
        try
        {
            page_.connectTarget(targetId_,
                                std::min(timeout, std::chrono::milliseconds(2000)),
                                stopRequested);
            break;
        }
        catch (...)
        {
            if (stopWasRequested(stopRequested))
            {
                close();
                throw std::runtime_error("cdp_cancelled");
            }
            if (attempt == 19)
            {
                close();
                throw;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    }
    throwIfStopRequested(stopRequested);
    page_.command("Page.enable", Json::Value(Json::objectValue), timeout, stopRequested);
    throwIfStopRequested(stopRequested);
    page_.command("Runtime.enable", Json::Value(Json::objectValue), timeout, stopRequested);
    throwIfStopRequested(stopRequested);
    Json::Value networkParams;
    networkParams["maxTotalBufferSize"] = static_cast<Json::UInt64>(maximumTotalResponseBodyBytes);
    networkParams["maxResourceBufferSize"] = static_cast<Json::UInt64>(maximumResponseBodyBytes);
    page_.command("Network.enable", networkParams, timeout, stopRequested);
    throwIfStopRequested(stopRequested);
    page_.command("DOM.enable", Json::Value(Json::objectValue), timeout, stopRequested);
    throwIfStopRequested(stopRequested);
}

void BrowserSession::close() noexcept
{
    try { page_.stop(); } catch (...) {}
    if (!targetId_.empty())
    {
        try
        {
            Json::Value params;
            params["targetId"] = targetId_;
            browser_.command("Target.closeTarget", params, std::chrono::seconds(3));
        }
        catch (...) {}
    }
    if (!contextId_.empty())
    {
        try
        {
            Json::Value params;
            params["browserContextId"] = contextId_;
            browser_.command("Target.disposeBrowserContext", params, std::chrono::seconds(3));
        }
        catch (...) {}
    }
    targetId_.clear();
    contextId_.clear();
    try { browser_.stop(); } catch (...) {}
}

void BrowserSession::setCookies(const Json::Value &cookies, std::chrono::milliseconds timeout)
{
    if (!cookies.isArray()) throw std::runtime_error("invalid_payload");
    Json::Value params;
    params["cookies"] = cookies;
    page_.command("Network.setCookies", params, timeout);
}

void BrowserSession::setExtraHeaders(const Json::Value &headers, std::chrono::milliseconds timeout)
{
    if (!headers.isObject()) throw std::runtime_error("invalid_payload");
    Json::Value params;
    params["headers"] = headers;
    page_.command("Network.setExtraHTTPHeaders", params, timeout);
}

void BrowserSession::navigate(const std::string &url, std::chrono::milliseconds timeout,
                              const StopPredicate &stopRequested)
{
    throwIfStopRequested(stopRequested);
    Json::Value params;
    params["url"] = url;
    const Json::Value result = page_.command("Page.navigate", params, timeout, stopRequested);
    throwIfStopRequested(stopRequested);
    if (result.isMember("errorText") && !result["errorText"].asString().empty())
        throw std::runtime_error("navigation_timeout");
    try
    {
        page_.waitForEvent("Page.loadEventFired", timeout, stopRequested);
        throwIfStopRequested(stopRequested);
    }
    catch (const std::runtime_error &error)
    {
        const bool cancelled = stopWasRequested(stopRequested) ||
                               std::string(error.what()) == "cdp_cancelled";
        try { stopLoading(); } catch (...) {}
        if (cancelled) throw std::runtime_error("cdp_cancelled");
        throw std::runtime_error("navigation_timeout");
    }
}

void BrowserSession::stopLoading(std::chrono::milliseconds timeout)
{
    page_.command("Page.stopLoading", Json::Value(Json::objectValue), timeout);
}

bool BrowserSession::waitForSelector(const std::string &selector,
                                     std::chrono::milliseconds timeout)
{
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline)
    {
        try
        {
            Json::Value docParams;
            docParams["depth"] = 1;
            const Json::Value doc = page_.command("DOM.getDocument", docParams, std::chrono::seconds(2));
            Json::Value query;
            query["nodeId"] = doc["root"].get("nodeId", 0).asInt();
            query["selector"] = selector;
            if (query["nodeId"].asInt() > 0 &&
                page_.command("DOM.querySelector", query, std::chrono::seconds(2))
                        .get("nodeId", 0).asInt() > 0) return true;
        }
        catch (...) {}
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    return false;
}

bool BrowserSession::waitForAnySelector(const std::vector<std::string> &selectors,
                                        std::chrono::milliseconds timeout)
{
    if (selectors.empty()) return true;
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline)
    {
        for (const auto &selector : selectors)
            if (waitForSelector(selector, std::chrono::milliseconds(150))) return true;
    }
    return false;
}

std::vector<int> BrowserSession::querySelectorAll(const std::string &selector,
                                                   std::chrono::milliseconds timeout)
{
    Json::Value docParams;
    docParams["depth"] = 1;
    const Json::Value doc = page_.command("DOM.getDocument", docParams, timeout);
    Json::Value query;
    query["nodeId"] = doc["root"].get("nodeId", 0).asInt();
    query["selector"] = selector;
    const Json::Value result = page_.command("DOM.querySelectorAll", query, timeout);
    std::vector<int> nodes;
    for (const auto &node : result["nodeIds"]) nodes.push_back(node.asInt());
    return nodes;
}

std::string BrowserSession::getOuterHtml(int nodeId, std::chrono::milliseconds timeout)
{
    Json::Value params;
    params["nodeId"] = nodeId;
    return page_.command("DOM.getOuterHTML", params, timeout).get("outerHTML", "").asString();
}

Json::Value BrowserSession::evaluate(const std::string &expression,
                                     std::chrono::milliseconds timeout)
{
    const Json::Value result = page_.command("Runtime.evaluate", paramsWithTimeout(expression), timeout);
    if (result.isMember("exceptionDetails")) throw std::runtime_error("invalid_payload");
    return result["result"].get("value", Json::Value());
}

Json::Value BrowserSession::getCookies(std::chrono::milliseconds timeout)
{
    return page_.command("Network.getCookies", Json::Value(Json::objectValue), timeout)
        .get("cookies", Json::Value(Json::arrayValue));
}

Json::Value BrowserSession::captureSnapshot(std::chrono::milliseconds timeout)
{
    Json::Value params;
    params["computedStyles"] = Json::Value(Json::arrayValue);
    params["includePaintOrder"] = false;
    params["includeDOMRects"] = false;
    return page_.command("DOMSnapshot.captureSnapshot", params, timeout);
}

void BrowserSession::boundedScroll(int steps, std::chrono::milliseconds pause,
                                   std::chrono::milliseconds commandTimeout,
                                   const std::function<bool()> &stopRequested)
{
    int stalls = 0;
    for (int step = 0; step < std::max(0, steps) && stalls < 3; ++step)
    {
        if (stopRequested && stopRequested()) return;
        const auto before = evaluate("document.documentElement.scrollHeight", commandTimeout).asInt64();
        if (stopRequested && stopRequested()) return;
        evaluate("(()=>{window.scrollBy(0,Math.max(window.innerHeight*2,1200));return true;})()",
                 commandTimeout);
        if (stopRequested && stopRequested()) return;
        std::this_thread::sleep_for(pause);
        if (stopRequested && stopRequested()) return;
        const auto after = evaluate("document.documentElement.scrollHeight", commandTimeout).asInt64();
        stalls = after <= before ? stalls + 1 : 0;
    }
}

std::vector<Json::Value> BrowserSession::responseBodies(const std::vector<std::string> &urlNeedles,
                                                        std::size_t maximum,
                                                        std::chrono::milliseconds timeout,
                                                        const std::function<bool()> &stopRequested)
{
    std::vector<Json::Value> output;
    if (maximum == 0 || urlNeedles.empty()) return output;
    // Modern social pages load many scripts and images before their feed JSON.
    // Inspect the complete bounded CDP queue, then retain at most `maximum`
    // matching response bodies. Looking at only the first 4x events regularly
    // discarded TikTok item-list responses that arrived later.
    constexpr std::size_t eventLimit = 2048;
    std::size_t totalBodyBytes = 0;
    for (const auto &event : page_.takeEvents("Network.responseReceived", eventLimit))
    {
        if (stopRequested && stopRequested()) break;
        if (output.size() >= maximum) break;
        const std::string url = event["response"].get("url", "").asString();
        const std::string mime = event["response"].get("mimeType", "").asString();
        if (!containsAny(url, urlNeedles) || mime.find("json") == std::string::npos) continue;
        const Json::Value &headers = event["response"]["headers"];
        const Json::Value &contentLength = headers.isMember("content-length")
            ? headers["content-length"] : headers["Content-Length"];
        if (contentLength.isNumeric() && contentLength.asUInt64() > maximumResponseBodyBytes)
            continue;
        if (contentLength.isString())
        {
            const std::string value = contentLength.asString();
            if (!value.empty() && value.size() < 20 &&
                std::all_of(value.begin(), value.end(), [](unsigned char c) { return std::isdigit(c); }) &&
                std::stoull(value) > maximumResponseBodyBytes) continue;
        }
        Json::Value params;
        params["requestId"] = event.get("requestId", "").asString();
        if (params["requestId"].asString().empty()) continue;
        try
        {
            const Json::Value body = page_.command("Network.getResponseBody", params, timeout);
            if (stopRequested && stopRequested()) break;
            if (!body.get("base64Encoded", false).asBool())
            {
                Json::CharReaderBuilder builder;
                Json::Value parsed;
                std::string errors;
                const std::string raw = body.get("body", "").asString();
                if (raw.size() > maximumResponseBodyBytes ||
                    totalBodyBytes + raw.size() > maximumTotalResponseBodyBytes) continue;
                totalBodyBytes += raw.size();
                const std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
                if (reader->parse(raw.data(), raw.data() + raw.size(), &parsed, &errors))
                    output.push_back(std::move(parsed));
            }
        }
        catch (...) {}
    }
    return output;
}

void BrowserSession::clearOriginData(const std::string &origin, std::chrono::milliseconds timeout)
{
    Json::Value params;
    params["origin"] = origin;
    params["storageTypes"] = "all";
    page_.command("Storage.clearDataForOrigin", params, timeout);
}

Json::Value BrowserSession::loadCookiesFile(const std::string &path)
{
    std::ifstream input(path);
    if (!input) throw std::runtime_error("auth_required");
    Json::CharReaderBuilder builder;
    Json::Value cookies;
    std::string errors;
    if (!Json::parseFromStream(builder, input, &cookies, &errors) || !cookies.isArray() || cookies.empty())
        throw std::runtime_error("auth_required");
    Json::Value normalized(Json::arrayValue);
    for (const auto &raw : cookies)
    {
        if (!raw.isObject() || raw.get("name", "").asString().empty()) continue;
        Json::Value cookie;
        for (const char *key : {"name", "value", "domain", "path", "url", "httpOnly", "secure"})
            if (raw.isMember(key) && !raw[key].isNull()) cookie[key] = raw[key];
        if (!cookie.isMember("path")) cookie["path"] = "/";
        if (raw["expires"].isNumeric()) cookie["expires"] = raw["expires"];
        else if (raw["expirationDate"].isNumeric()) cookie["expires"] = raw["expirationDate"];
        if (raw["sameSite"].isString())
        {
            std::string sameSite = raw["sameSite"].asString();
            std::transform(sameSite.begin(), sameSite.end(), sameSite.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
            if (sameSite == "strict") cookie["sameSite"] = "Strict";
            else if (sameSite == "lax") cookie["sameSite"] = "Lax";
            else if (sameSite == "none" || sameSite == "no_restriction") cookie["sameSite"] = "None";
        }
        normalized.append(std::move(cookie));
    }
    if (normalized.empty()) throw std::runtime_error("auth_required");
    return normalized;
}

}  // namespace argus::browser
