#include "PlatformAdapter.h"

namespace argus::browser {

std::unique_ptr<PlatformAdapter> makeVkAdapter()
{
    AdapterSpec spec;
    spec.platform = "vk";
    spec.cookieEnvironment = "VK_COOKIES_PATH";
    spec.allowedHosts = {"vk.com", ".vk.com", "vk.ru", ".vk.ru"};
    spec.selectors = {".post", ".wall_item", "a[href*='wall']"};
    spec.networkUrlNeedles = {"wall.get", "al_wall.php"};
    spec.scrollSteps = 20;
    spec.scrollPauseMs = 1500;
    // Extract only the authored post body. `.post_info`/`el.innerText` also
    // contain localized VK chrome (likes, shares, comments), which previously
    // made a Russian image-only post appear as Ukrainian interface text.
    spec.extractionExpression = R"JS((()=>{
  const rows = [];
  for (const card of document.querySelectorAll('.post,.wall_item,article,[data-post-id]')) {
    if (!card.dataset.postId && card.parentElement?.closest('[data-post-id],.post,.wall_item')) continue;
    const id = (card.dataset.postId || card.id || '').match(/^(?:post|wall)?(-?\d+_\d+)$/)?.[1];
    const link = card.querySelector('.PostHeaderSubtitle__link[href*="wall"],a.post_link[href*="wall"],a.wi_date[href*="wall"],a[href*="wall"]:has(time)');
    const key = id || link?.pathname.match(/^\/wall(-?\d+_\d+)$/)?.[1];
    const author = card.querySelector('.PostHeaderTitle a[href],.post_author[href],.post_author a[href],.pi_author[href],header a[href]');
    const text = card.querySelector('.wall_post_text,.wall_post_text_zoomable,.pi_text,.post_text,[data-testid="post_text"]');
    const stamp = card.querySelector('.PostHeaderSubtitle__link time,.rel_date[time],time[datetime],.post_date[data-time]');
    const media = card.querySelectorAll('.page_post_sized_thumbs img[src],.page_post_thumb_wrap img[src],.wall_album img[src],.MediaGrid img[src],.post_content img[src],.pi_medias img[src]');
    rows.push({url:key ? new URL('/wall'+key,location.href).href : '', ownerUrl:author?.href || '', pageUrl:location.href,
      text:(text?.innerText || '').trim(), publishedAt:stamp?.getAttribute('datetime') || stamp?.getAttribute('time') || stamp?.dataset.time || '',
      images:Array.from(media, n=>n.src), videos:Array.from(card.querySelectorAll('video[src],video[poster]'),n=>n.currentSrc||n.src||n.poster)});
  }
  return rows;
})())JS";
    spec.canonicalizer = [hosts = spec.allowedHosts](const std::string &url) {
        if (!allowedHttpUrl(url, hosts)) return std::pair<std::string, std::string>{};
        const std::string owner = regexCapture(url, R"(wall(-?\d+)_(\d+))", 1);
        const std::string post = regexCapture(url, R"(wall(-?\d+)_(\d+))", 2);
        if (owner.empty() || post.empty()) return std::pair<std::string, std::string>{};
        return std::make_pair("vk:" + owner + ":" + post,
                              "https://vk.com/wall" + owner + "_" + post);
    };
    return makeDomAdapter(std::move(spec));
}

}  // namespace argus::browser
