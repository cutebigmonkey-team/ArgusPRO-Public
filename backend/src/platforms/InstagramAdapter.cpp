#include "PlatformAdapter.h"

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeInstagramAdapter()
{
    AdapterSpec spec;
    spec.platform = "instagram";
    spec.cookieEnvironment = "INSTAGRAM_COOKIES_PATH";
    spec.allowedHosts = {"instagram.com", "www.instagram.com"};
    spec.selectors = {"article", "a[href*='/p/']", "a[href*='/reel/']"};
    spec.networkUrlNeedles = {"/graphql/query", "/api/v1/feed/", "/api/v1/media"};
    spec.scrollSteps = 60;
    spec.scrollPauseMs = 1200;
    spec.networkIdentityFields = {"shortcode", "code"};
    spec.networkTimestampFields = {"taken_at", "taken_at_timestamp", "timestamp"};
    spec.networkUrlTemplate = "https://www.instagram.com/p/{id}/";
    spec.extractionExpression = R"JS((()=>Array.from(document.querySelectorAll("article,a[href*='/p/'],a[href*='/reel/']")).map((el)=>{const a=el.matches('a')?el:el.querySelector("a[href*='/p/'],a[href*='/reel/']");const root=el.matches('a')?el.closest('article')||el:el;const t=root.querySelector?.('time');return {url:a?.href||'',text:(root.innerText||'').trim(),publishedAt:t?.getAttribute('datetime')||'',images:Array.from(root.querySelectorAll?.('img[src]')||[]).map(x=>x.src).slice(0,8),videos:Array.from(root.querySelectorAll?.('video[src],video[poster]')||[]).map(x=>x.src||x.poster).filter(Boolean).slice(0,4)}}).filter(x=>x.url))())JS";
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string id = regexCapture(url, R"(/(?:p|reel)/([A-Za-z0-9_-]+))");
        if (id.empty()) return std::pair<std::string, std::string>{};
        return std::make_pair("instagram:" + id, stripQueryAndFragment(url));
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
