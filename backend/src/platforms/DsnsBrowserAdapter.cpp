#include "PlatformAdapter.h"

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeDsnsBrowserAdapter()
{
    AdapterSpec spec;
    spec.platform = "dsns-browser";
    spec.allowedHosts = {"dsns.gov.ua", ".dsns.gov.ua"};
    spec.selectors = {"article", ".news-item", ".blog-item", ".item-page"};
    spec.networkUrlNeedles = {"/api/", "format=json"};
    spec.scrollSteps = 5;
    spec.scrollPauseMs = 750;
    spec.extractionExpression = R"JS((()=>Array.from(document.querySelectorAll("article,.news-item,.blog-item,.item")).map((el)=>{const a=el.querySelector('a[href]');const t=el.querySelector('time');return {url:a?.href||'',text:(el.innerText||'').trim(),publishedAt:t?.getAttribute('datetime')||'',images:Array.from(el.querySelectorAll('img[src]')).map(x=>x.src).slice(0,8),videos:Array.from(el.querySelectorAll('video[src],video[poster]')).map(x=>x.src||x.poster).filter(Boolean).slice(0,4)}}).filter(x=>x.url))())JS";
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string canonical = stripQueryAndFragment(url);
        if (canonical.empty()) return std::pair<std::string, std::string>{};
        return std::make_pair("dsns-browser:" + canonical, canonical);
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
