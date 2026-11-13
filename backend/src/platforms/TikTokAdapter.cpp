#include "PlatformAdapter.h"

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeTikTokAdapter()
{
    AdapterSpec spec;
    spec.platform = "tiktok";
    spec.cookieEnvironment = "TIKTOK_COOKIES_PATH";
    spec.allowedHosts = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vt.tiktok.com"};
    spec.selectors = {"a[href*='/video/']", "[data-e2e='user-post-item']"};
    spec.networkUrlNeedles = {"/api/post/item_list", "/api/item/detail"};
    // TikTok intermittently serves a tiny script-only challenge shell with a
    // normal 200 status. Keep the profile probe bounded so an async challenge
    // transition cannot hold Chromium in a long scroll cycle.
    spec.scrollSteps = 3;
    spec.scrollPauseMs = 1500;
    spec.networkIdentityFields = {"aweme_id", "id"};
    spec.networkTimestampFields = {"create_time", "createTime", "timestamp"};
    spec.networkUrlTemplate = "https://www.tiktok.com/@i/video/{id}";
    spec.extractionExpression = R"JS((()=>{try{
const sourceMatch=decodeURIComponent(location.pathname).match(/^\/@([A-Za-z0-9._-]+)\/?$/),sourceHandle=String(sourceMatch?.[1]||'').toLowerCase();if(!sourceHandle)return [];
const out=[],seen=new Set(),add=(url,text,publishedAt,images=[],videos=[])=>{const original=String(url||''),match=original.match(/^https?:\/\/(?:www\.)?tiktok\.com\/@([^/?#]+)\/video\/(\d{8,})(?:\/?(?:[?#].*)?)$/i);if(!match||match[1].toLowerCase()!==sourceHandle||seen.has(match[2])||out.length>=50)return;seen.add(match[2]);out.push({url:original,text:String(text||'').trim(),publishedAt:String(publishedAt||''),images:Array.from(images||[]).filter(x=>String(x).startsWith('http')).slice(0,8),videos:Array.from(videos||[]).filter(x=>String(x).startsWith('http')).slice(0,4)})};
for(const a of document.querySelectorAll("a[href*='/video/']")){const el=a.closest("article,[data-e2e='user-post-item'],div")||a,t=el.querySelector?.('time');add(a.href,el.innerText||a.getAttribute('aria-label')||'',t?.getAttribute('datetime')||'',Array.from(el.querySelectorAll?.('img[src]')||[]).map(x=>x.src),Array.from(el.querySelectorAll?.('video[src],video[poster]')||[]).map(x=>x.src||x.poster))}
const walk=(v,d=0)=>{if(d>20||out.length>=50||v==null)return;if(Array.isArray(v)){for(const x of v)walk(x,d+1);return}if(typeof v!=='object')return;const authorHandle=String(typeof v.author==='string'?v.author:(v.author?.uniqueId||v.author?.unique_id||v.author?.uniqueName||v.author?.username||'')).toLowerCase(),id=String(v.aweme_id||v.id||''),ts=v.create_time||v.createTime||v.timestamp||'';if(authorHandle===sourceHandle&&/^\d{8,}$/.test(id)&&ts&&!seen.has(id)){const rawTs=String(ts),epoch=Number(rawTs),numeric=/^\d+$/.test(rawTs),publishedAt=numeric?(Number.isFinite(epoch)&&epoch>0&&epoch<4102444800?new Date(epoch*1000).toISOString():''):rawTs;add('https://www.tiktok.com/@'+authorHandle+'/video/'+id,v.desc||v.title||'',publishedAt)}for(const x of Object.values(v))walk(x,d+1)};
for(const selector of ['#__UNIVERSAL_DATA_FOR_REHYDRATION__','#SIGI_STATE','#__NEXT_DATA__']){const script=document.querySelector(selector);if(!script)continue;try{walk(JSON.parse(script.textContent||'{}'))}catch{}}return out}catch{return []}})())JS";
    spec.navigationTransform = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::string{};
        const std::string handle = regexCapture(
            httpUrlPath(url), R"(^/@([A-Za-z0-9._-]+)/?$)");
        if (handle.empty()) return std::string{};
        return "https://www.tiktok.com/@" + handle;
    };
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string path = httpUrlPath(url);
        const std::string handle = regexCapture(
            path, R"(^/@([A-Za-z0-9._-]+)/video/(\d{8,})/?$)", 1);
        const std::string id = regexCapture(
            path, R"(^/@([A-Za-z0-9._-]+)/video/(\d{8,})/?$)", 2);
        if (handle.empty() || id.empty()) return std::pair<std::string, std::string>{};
        return std::make_pair("tiktok:" + id,
                              "https://www.tiktok.com/@" + handle + "/video/" + id);
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
