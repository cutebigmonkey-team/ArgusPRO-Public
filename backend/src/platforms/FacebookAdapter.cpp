#include "PlatformAdapter.h"

#include <algorithm>
#include <cctype>
#include <regex>
#include <set>

namespace argus::browser {
namespace {

std::string lower(std::string value)
{
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char character) { return static_cast<char>(std::tolower(character)); });
    return value;
}

std::string queryValue(const std::string &url, const std::string &key)
{
    return regexCapture(url, "(?:[?&])" + key + "=([^&#]+)");
}

std::string normalizedPath(const std::string &url)
{
    std::string path = lower(httpUrlPath(url));
    while (path.size() > 1 && path.back() == '/') path.pop_back();
    return path;
}

struct FacebookSourceIdentity {
    std::string handle;
    std::string numericId;
    std::string groupId;

    bool valid() const { return !handle.empty() || !numericId.empty() || !groupId.empty(); }
};

FacebookSourceIdentity sourceIdentity(const std::string &sourceUrl)
{
    FacebookSourceIdentity source;
    const std::string path = normalizedPath(sourceUrl);
    if (path == "/profile.php")
    {
        source.numericId = queryValue(sourceUrl, "id");
        return source;
    }
    if (const std::string group = regexCapture(path, R"(^/groups/([^/?#]+)(?:/|$))"); !group.empty())
    {
        source.groupId = lower(group);
        return source;
    }
    if (const std::string pageId = regexCapture(path, R"(^/pages/[^/]+/(\d+)(?:/|$))"); !pageId.empty())
    {
        source.numericId = pageId;
        return source;
    }
    const std::string first = regexCapture(path, R"(^/([^/?#]+)(?:/|$))");
    static const std::set<std::string> reserved{
        "", "share", "story.php", "permalink.php", "photo.php", "watch", "reel", "reels",
        "videos", "posts", "login", "checkpoint", "groups", "pages"};
    if (!reserved.contains(first))
    {
        if (std::all_of(first.begin(), first.end(), [](unsigned char character) {
                return std::isalnum(character) || character == '.' || character == '_' || character == '-';
            }))
            source.handle = first;
    }
    return source;
}

bool isCommentIdentity(const std::string &identity)
{
    const std::string normalized = lower(identity);
    // Base64("comment:") starts with this stable prefix, with or without
    // padding and for both standard and URL-safe alphabets.
    return normalized.rfind("comment:", 0) == 0 ||
           normalized.rfind("comment_id:", 0) == 0 ||
           normalized.rfind("comment_id=", 0) == 0 ||
           normalized.rfind("reply_comment_id:", 0) == 0 ||
           normalized.rfind("reply_comment_id=", 0) == 0 ||
           identity.rfind("Y29tbWVudDo", 0) == 0;
}

bool hasCommentIdentity(const std::string &url)
{
    const std::string normalized = lower(url);
    return normalized.find("comment_id=") != std::string::npos ||
           normalized.find("reply_comment_id=") != std::string::npos;
}

std::string postIdentity(const std::string &url)
{
    if (hasCommentIdentity(url)) return {};
    std::string id = queryValue(url, "story_fbid");
    if (id.empty()) id = queryValue(url, "fbid");
    if (id.empty())
        id = regexCapture(httpUrlPath(url),
                          R"(/(?:posts|permalink|videos|reel|reels)/([A-Za-z0-9_-]+)(?:/|$))");
    if (id.empty() || isCommentIdentity(id)) return {};
    return id;
}

bool urlMatchesSource(const std::string &url, const FacebookSourceIdentity &source)
{
    if (url.empty()) return false;
    const std::string path = normalizedPath(url);
    if (!source.groupId.empty())
        return path.rfind("/groups/" + source.groupId + "/posts/", 0) == 0;
    if (!source.numericId.empty())
    {
        if (queryValue(url, "id") == source.numericId) return true;
        return path.rfind("/" + lower(source.numericId) + "/posts/", 0) == 0 ||
               path.rfind("/" + lower(source.numericId) + "/videos/", 0) == 0;
    }
    if (source.handle.empty()) return false;
    const std::string prefix = "/" + source.handle + "/";
    if (path.rfind(prefix, 0) != 0) return false;
    const std::string remainder = path.substr(prefix.size());
    return remainder.rfind("posts/", 0) == 0 || remainder.rfind("videos/", 0) == 0 ||
           remainder.rfind("reel/", 0) == 0 || remainder.rfind("reels/", 0) == 0 ||
           remainder.rfind("permalink/", 0) == 0;
}

bool ownerMatchesSource(const Json::Value &row, const FacebookSourceIdentity &source)
{
    const std::string ownerId = row.get("ownerId", "").asString();
    if (!source.numericId.empty() && !ownerId.empty() && ownerId == source.numericId) return true;
    const std::string ownerUrl = row.get("ownerUrl", "").asString();
    if (ownerUrl.empty()) return false;
    if (!source.numericId.empty())
        return queryValue(ownerUrl, "id") == source.numericId ||
               normalizedPath(ownerUrl) == "/" + lower(source.numericId);
    if (!source.groupId.empty())
        return normalizedPath(ownerUrl).rfind("/groups/" + source.groupId, 0) == 0;
    return !source.handle.empty() && normalizedPath(ownerUrl) == "/" + source.handle;
}

bool sourceVerifiedRow(const Json::Value &row, const std::string &sourceUrl)
{
    if (!row.isObject()) return false;
    const FacebookSourceIdentity source = sourceIdentity(sourceUrl);
    if (!source.valid()) return false;
    const std::string typeName = lower(row.get("typename", "").asString());
    if (typeName == "comment" || typeName == "xfbcommentdeprecated") return false;
    const std::string identity = row.get("identity", "").asString();
    if (isCommentIdentity(identity)) return false;
    const std::string postUrl = row.get("url", "").asString();
    if (!allowedHttpUrl(postUrl, {"facebook.com", ".facebook.com"}) ||
        hasCommentIdentity(postUrl) || postIdentity(postUrl).empty()) return false;
    return urlMatchesSource(postUrl, source) || ownerMatchesSource(row, source);
}

std::string mbasicSourceUrl(const std::string &sourceUrl)
{
    if (!allowedHttpUrl(sourceUrl, {"facebook.com", ".facebook.com"}) ||
        !sourceIdentity(sourceUrl).valid()) return {};
    return std::regex_replace(sourceUrl, std::regex(R"(^https?://[^/]+)", std::regex::icase),
                              "https://mbasic.facebook.com");
}

}  // namespace

std::unique_ptr<PlatformAdapter> makeFacebookAdapter()
{
    AdapterSpec spec;
    spec.platform = "facebook";
    spec.cookieEnvironment = "FACEBOOK_COOKIES_PATH";
    spec.allowedHosts = {"facebook.com", ".facebook.com"};
    spec.selectors = {"article", "[role='article']", "[data-ft]", "a[href*='/reel/']",
                      "a[href*='/videos/']"};
    spec.networkUrlNeedles = {"/api/graphql", "/graphql"};
    spec.scrollSteps = 40;
    spec.scrollPauseMs = 1500;
    spec.networkIdentityFields = {"post_id", "id"};
    spec.networkTimestampFields = {"creation_time", "created_time", "published_time", "timestamp"};
    spec.sourceVerifier = sourceVerifiedRow;
    spec.extractionExpression = R"JS((()=>Array.from(document.querySelectorAll("article,[role='article'],[data-ft]")).map((el)=>{const a=el.querySelector("a[href*='story_fbid=']:not([href*='comment_id=']),a[href*='/posts/']:not([href*='comment_id=']),a[href*='/permalink/']:not([href*='comment_id=']),a[href*='fbid=']:not([href*='comment_id=']),a[href*='/reel/']:not([href*='comment_id=']),a[href*='/reels/']:not([href*='comment_id=']),a[href*='/videos/']:not([href*='comment_id='])");const owners=Array.from(el.querySelectorAll("h1 a[href],h2 a[href],h3 a[href],strong a[href]"));const owner=owners.find(x=>x!==a&&!/story_fbid=|fbid=|\/posts\/|\/permalink\/|\/reels?\/|\/videos\//i.test(x.href||''));let ft={};try{ft=JSON.parse(el.getAttribute('data-ft')||'{}')}catch{}const t=el.querySelector("time,abbr[data-utime]");const raw=t?.getAttribute('datetime')||t?.getAttribute('data-utime')||'';return {url:a?.href||'',ownerUrl:owner?.href||'',ownerId:String(ft.page_id||ft.content_owner_id_new||ft.profile_id||''),text:(el.innerText||'').trim(),publishedAt:/^\d+$/.test(raw)?new Date(Number(raw)*1000).toISOString():raw,images:Array.from(el.querySelectorAll('img[src]')).map(x=>x.src).slice(0,8),videos:Array.from(el.querySelectorAll('video[src],video[poster]')).map(x=>x.src||x.poster).filter(Boolean).slice(0,4)}}).filter(x=>x.url))())JS";
    spec.fallbackThreshold = 2;
    spec.fallbackNavigationTransform = mbasicSourceUrl;
    spec.fallbackSelectors = {"[data-ft]", "abbr[data-utime]"};
    // mbasic is a static, bounded second pass. Only top-level story containers
    // are considered, and C++ sourceVerifier rejects every row that cannot be
    // tied to the requested page/group through its permalink, author URL or
    // numeric owner id.
    spec.fallbackExtractionExpression = R"JS((()=>Array.from(document.querySelectorAll("[data-ft]")).filter(el=>!el.parentElement?.closest('[data-ft]')).map((el)=>{let ft={};try{ft=JSON.parse(el.getAttribute('data-ft')||'{}')}catch{}const a=el.querySelector("a[href*='story_fbid=']:not([href*='comment_id=']),a[href*='/posts/']:not([href*='comment_id=']),a[href*='/permalink/']:not([href*='comment_id=']),a[href*='fbid=']:not([href*='comment_id=']),a[href*='/reel/']:not([href*='comment_id=']),a[href*='/reels/']:not([href*='comment_id=']),a[href*='/videos/']:not([href*='comment_id='])");const owners=Array.from(el.querySelectorAll("h1 a[href],h2 a[href],h3 a[href],strong a[href]"));const owner=owners.find(x=>x!==a&&!/story_fbid=|fbid=|\/posts\/|\/permalink\/|\/reels?\/|\/videos\//i.test(x.href||''));const t=el.querySelector('abbr[data-utime],time');const raw=t?.getAttribute('data-utime')||t?.getAttribute('datetime')||'';return {url:a?.href||'',ownerUrl:owner?.href||'',ownerId:String(ft.page_id||ft.content_owner_id_new||ft.profile_id||''),text:Array.from(el.querySelectorAll('p')).map(x=>(x.innerText||'').trim()).filter(Boolean).join('\n')||(el.innerText||'').trim(),publishedAt:/^\d+$/.test(raw)?new Date(Number(raw)*1000).toISOString():raw,images:Array.from(el.querySelectorAll('img[src]')).map(x=>x.src).slice(0,8),videos:[]}}).filter(x=>x.url))())JS";
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts) || hasCommentIdentity(url))
            return std::pair<std::string, std::string>{};
        const std::string id = postIdentity(url);
        if (id.empty()) return std::pair<std::string, std::string>{};
        const std::string path = lower(httpUrlPath(url));
        const std::string canonical = path.find("story.php") != std::string::npos ||
                                      path.find("photo") != std::string::npos
            ? stripFragment(url) : stripQueryAndFragment(url);
        return std::make_pair("facebook:" + id, canonical);
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
