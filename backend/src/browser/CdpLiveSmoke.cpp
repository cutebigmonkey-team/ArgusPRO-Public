#include "BrowserSession.h"
#include "CdpClient.h"
#include "PlatformAdapter.h"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <string>

int main()
{
    using namespace std::chrono_literals;
    const char *configured = std::getenv("ARGUS_CDP_ENDPOINT");
    const std::string endpoint = configured && *configured
        ? configured : "http://argus-chromium:9222";
    std::string stage = "version";
    try
    {
        argus::browser::CdpClient http(endpoint);
        const Json::Value version = http.version(5s);
        if (version.get("webSocketDebuggerUrl", "").asString().empty())
            throw std::runtime_error("version_missing_websocket");
        stage = "http_new";
        const Json::Value created = http.createTargetViaHttp("about:blank", 5s);
        const std::string httpTarget = created.get("id", "").asString();
        if (httpTarget.empty()) throw std::runtime_error("http_target_missing");
        stage = "http_list";
        bool listed = false;
        for (const auto &target : http.targets(5s))
            listed = listed || target.get("id", "").asString() == httpTarget;
        stage = "http_close";
        if (!listed || !http.closeTargetViaHttp(httpTarget, 5s))
            throw std::runtime_error("http_target_lifecycle_failed");

        stage = "session_open";
        argus::browser::BrowserSession session(endpoint);
        session.open(10s);
        stage = "navigate";
        session.navigate("data:text/html,<main><article%20id='post'>Argus%20CDP</article></main>", 10s);
        stage = "dom";
        if (!session.waitForSelector("#post", 3s))
            throw std::runtime_error("selector_not_found");
        const auto nodes = session.querySelectorAll("article", 3s);
        if (nodes.size() != 1 || session.getOuterHtml(nodes.front(), 3s).find("Argus CDP") == std::string::npos)
            throw std::runtime_error("dom_failed");
        if (session.evaluate("document.querySelector('#post').textContent", 3s).asString() != "Argus CDP")
            throw std::runtime_error("runtime_failed");
        stage = "adapter_expressions";
        constexpr const char *platforms[]{
            "facebook", "instagram", "x", "threads", "tiktok",
            "vk", "ok", "max", "dsns-browser"};
        for (const char *platform : platforms)
        {
            const auto adapter = argus::browser::makePlatformAdapter(platform);
            if (!adapter ||
                !session.evaluate(adapter->spec().extractionExpression, 3s).isArray())
                throw std::runtime_error(std::string("adapter_expression_failed:") + platform);
            if (!adapter->spec().fallbackExtractionExpression.empty() &&
                !session.evaluate(adapter->spec().fallbackExtractionExpression, 3s).isArray())
                throw std::runtime_error(std::string("adapter_fallback_expression_failed:") + platform);
        }
        stage = "snapshot";
        if (!session.captureSnapshot(5s).isObject())
            throw std::runtime_error("snapshot_failed");
        stage = "network_storage";
        (void)session.getCookies(3s);
        session.clearOriginData("https://example.invalid", 3s);
        session.close();
        std::cout << "live CDP smoke passed\n";
        return 0;
    }
    catch (const std::exception &error)
    {
        std::cerr << "live CDP smoke failed at " << stage << ": " << error.what() << '\n';
        return 1;
    }
}
