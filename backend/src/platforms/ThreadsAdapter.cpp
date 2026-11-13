#include "PlatformAdapter.h"

#include <algorithm>
#include <cctype>

namespace argus::browser {

namespace {

std::string lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return value;
}

std::string profileHandle(const std::string &url)
{
    return lower(regexCapture(httpUrlPath(url), R"(^/@([A-Za-z0-9._-]+)/?$)"));
}

std::string postHandle(const std::string &url)
{
    return lower(regexCapture(
        httpUrlPath(url), R"(^/@([A-Za-z0-9._-]+)/post/[A-Za-z0-9_-]+/?$)"));
}

}  // namespace

std::unique_ptr<PlatformAdapter> makeThreadsAdapter()
{
    AdapterSpec spec;
    spec.platform = "threads";
    spec.cookieEnvironment = "THREADS_COOKIES_PATH";
    spec.allowedHosts = {"threads.net", "www.threads.net", "threads.com", "www.threads.com"};
    spec.selectors = {"article", "a[href*='/post/']"};
    spec.networkUrlNeedles = {"/graphql", "Barcelona"};
    spec.scrollSteps = 60;
    spec.scrollPauseMs = 1500;
    spec.networkIdentityFields = {"code", "pk", "id"};
    spec.networkTimestampFields = {"taken_at", "taken_at_timestamp", "created_at", "timestamp"};
    spec.networkUrlTemplate = "https://www.threads.com/@i/post/{id}";
    spec.extractionExpression = R"JS((()=>Array.from(document.querySelectorAll("a[href*='/post/']")).map((a)=>{const el=a.closest("article,[role='article'],[data-pressable-container]")||a.parentElement||a;const t=el.querySelector?.('time');return {url:a.href||'',text:(el.innerText||'').trim(),publishedAt:t?.getAttribute('datetime')||'',images:Array.from(el.querySelectorAll?.('img[src]')||[]).map(x=>x.src).slice(0,8),videos:Array.from(el.querySelectorAll?.('video[src],video[poster]')||[]).map(x=>x.src||x.poster).filter(Boolean).slice(0,4)}}).filter(x=>x.url))())JS";
    spec.navigationTransform = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::string{};
        const std::string handle = profileHandle(url);
        if (handle.empty()) return std::string{};
        return "https://www.threads.com/@" + handle;
    };
    spec.sourceVerifier = [](const Json::Value &row, const std::string &sourceUrl) {
        const std::string expected = profileHandle(sourceUrl);
        const std::string actual = postHandle(row.get("url", "").asString());
        const std::string owner = lower(row.get("ownerHandle", "").asString());
        return !expected.empty() && actual == expected &&
               (owner.empty() || owner == expected);
    };
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string path = httpUrlPath(url);
        const std::string handle = regexCapture(
            path, R"(^/@([A-Za-z0-9._-]+)/post/([A-Za-z0-9_-]+)/?$)", 1);
        const std::string id = regexCapture(
            path, R"(^/@([A-Za-z0-9._-]+)/post/([A-Za-z0-9_-]+)/?$)", 2);
        if (handle.empty() || id.empty()) return std::pair<std::string, std::string>{};
        return std::make_pair("threads:" + id,
                              "https://www.threads.com/@" + handle + "/post/" + id);
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
