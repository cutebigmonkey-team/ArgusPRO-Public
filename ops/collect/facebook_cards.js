// Read only post content inside the rendered public story that owns the permalink.
(async () => {
  const storyCards = 'article,[role="article"],.userContentWrapper';
  const cards = document.querySelector(storyCards) ? storyCards : '[data-ft*="top_level_post_id"]';
  const rows = [];
  for (const card of document.querySelectorAll(cards)) {
    if (card.parentElement?.closest(cards)) continue;
    const within = selector => [...card.querySelectorAll(selector)].filter(node =>
      !node.closest('[role="comment"]') && node.closest(cards) === card);
    const links = within('a[href]');
    const permalink = links.find(link =>
      /\/(posts|permalink|videos|reels?)\/|[?&]story_fbid=/.test(link.href) &&
      !/[?&](?:reply_)?comment_id=/.test(link.href));
    if (!permalink) continue;
    const messages = within('[data-ad-preview="message"],[data-ad-comet-preview="message"],.userContent,.text_exposed_root');
    let expanded = false;
    for (const message of messages) {
      const button = Array.from(message.querySelectorAll('button,[role="button"]')).find(el =>
        !el.closest('a[href]') && /^(see more|show more|більше|показати більше|ещё|показать ещё)$/iu.test(el.innerText.trim()));
      if (button) {
        const length = message.innerText.length;
        button.click();
        await new Promise(resolve => setTimeout(resolve, 180));
        expanded ||= message.isConnected && !message.contains(button) && message.innerText.length > length;
      }
    }
    const blocks = messages.length ? messages : within('p').filter(node => !node.closest('a,header,footer'));
    const text = [...new Set(blocks.map(node => node.innerText?.trim()).filter(Boolean))].join('\n');
    const videos = within('video').map(el => el.currentSrc || el.poster).filter(Boolean);
    const images = within('a[href*="/photo"] img[src],a[href*="fbid="] img[src]').map(el => el.src);
    if (!text && !videos.length && !images.length) continue;
    const author = within('h2 a[href],h3 a[href],strong a[href]').find(a => a !== permalink);
    let metadata = {};
    try { metadata = JSON.parse(card.getAttribute('data-ft') || '{}'); } catch {}
    const stamps = within('header time[datetime],header [data-utime],abbr[data-utime],time[datetime]');
    const time = permalink.querySelector('time[datetime],[data-utime]') || (stamps.length === 1 ? stamps[0] : null);
    const stamp = time?.dateTime || time?.getAttribute('data-utime') || permalink.getAttribute('data-utime') || '';
    rows.push({url: permalink.href, ownerUrl: author?.href || '',
      ownerId: String(metadata.content_owner_id_new || metadata.page_id || ''),
      text, expanded, images, videos, publishedAt: /^\d{9,10}$/.test(stamp) ? new Date(Number(stamp)*1000).toISOString() : stamp});
  }
  return rows;
})()
