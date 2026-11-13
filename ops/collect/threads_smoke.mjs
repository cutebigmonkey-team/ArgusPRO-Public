// Offline cloud smoke: installed Chromium + the real upstream parser, any language.
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { _parse_posts, _clean_lines } from './dist/routes.js';

const browser = await chromium.launch({headless: true});
try {
  const page = await browser.newPage();
  await page.setContent('<article>example<br>1h<br>Повідомлення українською мовою<br>Translate<br>12<br>3</article>');
  const text = await page.locator('article').innerText();
  const rows = _parse_posts(_clean_lines(text), 'example',
    {mode: 'profile', maxPostsPerAccount: 1, postLanguageFilter: 'any'}, new Date('2026-10-30T10:00:00Z'));
  assert.equal(rows.length, 1);
  assert.match(rows[0].text, /Повідомлення українською мовою/);
  assert.equal(rows[0].author, 'example');
  console.log('Installed Chromium and upstream Threads parser: Ukrainian fixture passed');
} finally {
  await browser.close();
}
