#include "PlatformAdapter.h"

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeMaxAdapter()
{
    AdapterSpec spec;
    spec.platform = "max";
    spec.cookieEnvironment = "MAX_COOKIES_PATH";
    spec.allowedHosts = {"max.ru", "www.max.ru", "web.max.ru"};
    spec.selectors = {".history .messageWrapper", ".messageWrapper", ".message"};
    // The current web client transports message history over a WebSocket.
    // BrowserSession::responseBodies intentionally handles HTTP JSON only;
    // do not pretend that WebSocket frames are HTTP response bodies.
    spec.networkUrlNeedles = {};
    spec.scrollSteps = 10;
    spec.scrollPauseMs = 1500;
    // The current client does not expose a verified self-permalink or message
    // identifier in the rendered wrapper. Fail closed instead of treating a
    // user-posted /c/ link inside message text as the wrapper's identity.
    spec.extractionExpression = "(()=>[])()";
    spec.navigationTransform = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::string{};
        const std::string clean = stripQueryAndFragment(url);
        const std::string route = httpUrlPath(clean);
        if (route.size() <= 1) return std::string{};
        const bool nickname = !regexCapture(route, R"(^/([A-Za-z0-9._-]+)$)").empty();
        const bool post = !regexCapture(route, R"(^/c/(-?\d+)/([A-Za-z0-9_-]+)$)", 2).empty();
        if (!nickname && !post) return std::string{};
        return "https://web.max.ru" + route;
    };
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string clean = stripQueryAndFragment(url);
        const std::string route = httpUrlPath(clean);
        if (route.empty()) return std::pair<std::string, std::string>{};
        const std::string channel = regexCapture(route, R"(^/c/(-?\d+)/([A-Za-z0-9_-]+)$)", 1);
        const std::string message = regexCapture(route, R"(^/c/(-?\d+)/([A-Za-z0-9_-]+)$)", 2);
        if (!channel.empty() && !message.empty())
            return std::make_pair("max:" + channel + ":" + message,
                                  "https://max.ru/c/" + channel + "/" + message);
        return std::pair<std::string, std::string>{};
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
