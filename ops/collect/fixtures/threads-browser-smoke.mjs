import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { _clean_lines, _parse_posts } from './dist/routes.js';

const browser = await chromium.launch({
  headless: true,
  chromiumSandbox: true,
});

try {
  const page = await browser.newPage();

  await page.setContent(`
    <main>
      <div>example</div>
      <div>Threads</div>
      <article>
        <div>example</div>
        <div>2h</div>
        <div>Український допис</div>
        <div>15</div>
      </article>
      <div>Log in</div>
      <article>
        <div>example</div>
        <div>1h</div>
        <div>Не повинно з’явитися</div>
      </article>
    </main>
  `);

  const rendered = await page.locator('main').innerText();

  const posts = _parse_posts(
    _clean_lines(rendered),
    'example',
    {
      mode: 'profile',
      maxPostsPerAccount: 10,
      postLanguageFilter: 'any',
    },
    new Date('2026-11-14T12:00:00Z'),
  );

  assert.equal(posts.length, 1);
  assert.equal(posts[0].author, 'example');
  assert.equal(posts[0].text, 'Український допис');
  assert.equal(posts[0].metrics.likes, '15');
  assert.equal(posts[0].posted_at_iso, '2026-11-14T10:00:00.000Z');
  assert.ok(!rendered.includes('SECRET_TOKEN'));

  console.log('Ukrainian fixture passed');
} finally {
  await browser.close();
}
