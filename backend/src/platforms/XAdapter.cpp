#include "PlatformAdapter.h"

#include <regex>

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeXAdapter()
{
    AdapterSpec spec;
    spec.platform = "x";
    spec.cookieEnvironment = "X_COOKIES_PATH";
    spec.allowedHosts = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"};
    spec.selectors = {"article[data-testid='tweet']", "a[href*='/status/']"};
    spec.networkUrlNeedles = {"UserTweets", "UserMedia", "/graphql/"};
    spec.scrollSteps = 60;
    spec.scrollPauseMs = 1500;
    spec.extractionExpression = R"JS((()=>Array.from(document.querySelectorAll("article[data-testid='tweet']")).map(card=>{
        const time=card.querySelector("a[href*='/status/'] time");
        const link=time?.closest("a");
        const text=card.querySelector("[data-testid='tweetText']")?.innerText || "";
        const images=Array.from(card.querySelectorAll("[data-testid='tweetPhoto'] img")).map(img=>img.src);
        const videos=Array.from(card.querySelectorAll("video")).flatMap(video=>[video.currentSrc,video.poster]).filter(Boolean);
        return {url:link?.href || "",text:text.trim(),publishedAt:time?.dateTime || "",images,videos};
    }).filter(row=>row.url))())JS";
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string path = httpUrlPath(url);
        const std::string webId = regexCapture(path, R"(^/i/web/status/(\d+)/?$)");
        if (!webId.empty()) return std::make_pair("x:" + webId, "https://x.com/i/web/status/" + webId);
        const std::string id = regexCapture(path, R"(^/[A-Za-z0-9_]{1,15}/status/(\d+)/?$)");
        if (id.empty()) return std::pair<std::string, std::string>{};
        const std::string handle = regexCapture(path, R"(^/([^/]+)/status/)");
        return std::make_pair("x:" + id, "https://x.com/" + (handle.empty() ? "i" : handle) + "/status/" + id);
    };
    spec.sourceVerifier = [hosts = spec.allowedHosts](const Json::Value &row, const std::string &source) {
        if (!allowedHttpUrl(source, hosts) || !row["url"].isString()) return false;
        const std::string path = httpUrlPath(source);
        if (path == "/search") return !regexCapture(source, R"([?&]q=([^&#]+))").empty();
        const std::string handle = regexCapture(path, R"(^/([A-Za-z0-9_]{1,15})(?:/status/\d+)?/?$)");
        if (handle.empty()) return false;
        const std::string id = regexCapture(path, R"(/status/(\d+)/?$)");
        return std::regex_match(httpUrlPath(row["url"].asString()),
                                std::regex("/" + handle + "/status/" + (id.empty() ? "[0-9]+" : id) + "/?", std::regex::icase));
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
