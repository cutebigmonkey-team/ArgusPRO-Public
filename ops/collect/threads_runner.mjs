// Use the pinned threads-crawler parser in a bounded anonymous Crawlee job.
// The upstream main also sends Telegram messages and accepts account sessions;
// this entry point only reads one public target and writes a JSON result.
import { writeFile } from 'node:fs/promises';
import { PlaywrightCrawler, Configuration } from 'crawlee';
import { chromium } from 'playwright';
import { _parse_posts, _clean_lines } from './dist/routes.js';

if (process.env.GITHUB_ACTIONS !== 'true' || process.env.RUNNER_ENVIRONMENT !== 'github-hosted') {
  throw new Error('GitHub-hosted runner required');
}
const [target, count, output] = process.argv.slice(2);
const url = new URL(target);
const profile = /^\/@([A-Za-z0-9_.]+)\/?$/.exec(url.pathname);
const single = /^\/@([A-Za-z0-9_.]+)\/post\/([A-Za-z0-9_-]+)\/?$/.exec(url.pathname);
if (url.protocol !== 'https:' || !['www.threads.com', 'www.threads.net'].includes(url.hostname) ||
    (!profile && !single) || !/^\d{1,3}$/.test(count) || Number(count)<1 || Number(count)>100) throw new Error('Invalid source');
let result = {status: 'no_public_posts', rows: []};
const config = new Configuration({persistStorage: false});
const crawler = new PlaywrightCrawler({
  maxRequestsPerCrawl: 1, maxConcurrency: 1, maxRequestRetries: 0,
  useSessionPool: false, retryOnBlocked: false,
  navigationTimeoutSecs: 15, requestHandlerTimeoutSecs: 25,
  launchContext: {launcher: chromium, useIncognitoPages: true},
  preNavigationHooks: [async ({page}) => {
    await page.context().route('**/*', async route => {
      const request = route.request();
      const dest = new URL(request.url());
      // Only the platform and its own CDNs; no arbitrary link-preview hosts.
      const allowed = ['threads.com','threads.net','instagram.com','cdninstagram.com','fbcdn.net','facebook.com'];
      if (dest.protocol !== 'https:' || (dest.port && dest.port !== '443') ||
          !allowed.some(h => dest.hostname === h || dest.hostname.endsWith('.' + h)) ||
          ['image','media','font'].includes(request.resourceType())) return route.abort();
      return route.continue();
    });
  }],
  async requestHandler({page, response}) {
    if (response && response.status() >= 400) {
      result.status = 'http_' + response.status(); return;
    }
    await page.waitForTimeout(1500);
    const title = await page.title();
    if (/captcha|verify you are human|just a moment/i.test(title)) { result.status = 'challenge_required'; return; }
    if (/\/(login|accounts|checkpoint)/.test(new URL(page.url()).pathname)) { result.status = 'login_required'; return; }
    const cards = await page.locator('div[data-pressable-container="true"], [role="article"]').evaluateAll(nodes =>
      nodes.slice(0, 200).map(n => ({text: n.innerText || '', links: [...n.querySelectorAll('a[href]')].map(a => a.href)})));
    const seen = new Set();
    for (const card of cards) {
      const link = card.links.find(value => {
        try { const u = new URL(value); return ['www.threads.com','www.threads.net'].includes(u.hostname) &&
          /^\/@[^/]+\/post\/[A-Za-z0-9_-]+\/?$/.test(u.pathname); } catch { return false; }
      });
      if (!link || seen.has(link)) continue;
      const match = /^\/@([^/]+)\/post\/([^/]+)/.exec(new URL(link).pathname);
      const owner = (profile || single)[1];
      if (match[1].toLowerCase() !== owner.toLowerCase() || (single && match[2] !== single[2])) continue;
      const rows = _parse_posts(_clean_lines(card.text), owner,
        {mode: single ? 'thread' : 'profile', maxPostsPerAccount: 1, postLanguageFilter: 'any'}, new Date());
      if (!rows.length) continue;
      result.rows.push({...rows[0], post_url: link}); seen.add(link);
      if (result.rows.length >= Number(count)) break;
    }
    if (result.rows.length) result.status = 'ok';
    else if (/log in or sign up|log in to (see|view|continue)/i.test(await page.locator('body').innerText())) result.status = 'login_required';
  },
  async failedRequestHandler({response}, error) {
    result.error_detail = String(error.message).replace(/https?:\/\/\S+/g, '<url>').slice(0, 600);
    result.status = response?.status() >= 400 ? 'http_' + response.status() :
      /429/.test(error.message) ? 'http_429' : /403/.test(error.message) ? 'http_403' : 'browser_failed';
  }
}, config);
await crawler.run([target]);
await writeFile(output, JSON.stringify(result));
