#include "PlatformAdapter.h"

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeOkAdapter()
{
    AdapterSpec spec;
    spec.platform = "ok";
    spec.cookieEnvironment = "OK_COOKIES_PATH";
    spec.allowedHosts = {"ok.ru", "www.ok.ru", "m.ok.ru"};
    spec.selectors = {".feed-w", ".topic-text_content", "a[href*='/topic/']"};
    spec.networkUrlNeedles = {"cmd=PopLayerStatuses", "st.cmd"};
    spec.scrollSteps = 20;
    spec.scrollPauseMs = 1500;
    spec.extractionExpression = R"JS((()=>{
  const rows = [];
  for (const card of document.querySelectorAll('[data-feed-id],.feed-w,.group-feed_w,article')) {
    if (!card.hasAttribute('data-feed-id') && (card.closest('[data-feed-id]') || card.querySelector('[data-feed-id]'))) continue;
    let ctx = {};
    try { ctx = JSON.parse(card.dataset.seenParams || '{}').data || {}; } catch {}
    const topic = String(ctx.mediaTopicId || '');
    const links = Array.from(card.querySelectorAll('a[href*="/topic/"],a[href*="/statuses/"]'));
    const link = topic ? links.find(a=>a.pathname.split('/').filter(Boolean).pop()===topic) : links[0];
    const stamp = card.querySelector('time[datetime],[data-ts],[data-time],[data-date],.feed_date');
    const text = card.querySelector('.media-text_cnt,.media-text,.topic-text_content,.media-text-i,.feed-text');
    const author = card.querySelector('.feed-header a[href],.feed_author a[href],.feed_ava a[href]');
    const images = card.querySelectorAll('.media-block img[src],.photo-collage img[src],.media-photos img[src],a[href*="/topic/"] img[src]');
    rows.push({url:link?.href || '',ownerUrl:author?.href || '', pageUrl:location.href,
      groupId:ctx.feedLocation==='GROUP' && ctx.feedId===card.dataset.feedId && topic && ctx.isFriendsOnly===false ? String(ctx.ownerContentId||'') : '',
      privateCard:ctx.isFriendsOnly===true, text:(text?.innerText||'').trim(),
      publishedAt:stamp?.getAttribute('datetime')||stamp?.dataset.ts||stamp?.dataset.time||stamp?.dataset.date||'',
      images:Array.from(images,n=>n.src), videos:Array.from(card.querySelectorAll('video[src],video[poster]'),n=>n.currentSrc||n.src||n.poster)});
  }
  return rows;
})())JS";
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string id = regexCapture(url, R"(/(?:topic|statuses)/(\d+))");
        if (id.empty()) return std::pair<std::string, std::string>{};
        return std::make_pair("ok:" + id, stripQueryAndFragment(url));
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
