import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createWorkspaceData } from '../../frontend/cloud/workspace-data.mjs';

const [state, port = '18765'] = process.argv.slice(2);
if (!state || !/^\d+$/.test(port) || Number(port) < 1024 || Number(port) > 65535) {
  throw new Error('Usage: node ops/local-stage/web.mjs STATE_DIRECTORY [PORT]');
}
const archive = resolve(state, 'archive');
const read = async (name) => JSON.parse(await readFile(resolve(archive, name), 'utf8'));
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

function render(posts, updatedAt) {
  const items = posts.slice(0, 100).map((post) =>
    `<article data-post-id="${escapeHtml(post.id)}">
      <h2>${escapeHtml(post.title || post.sourceName)}</h2>
      <p>${escapeHtml(post.platform)} · ${escapeHtml(post.publishedAt || post.discoveredAt)}</p>
      <p>${escapeHtml(post.text.slice(0, 1200))}</p>
      ${post.url ? `<a href="${escapeHtml(post.url)}" rel="noreferrer">Original post</a>` : ''}
    </article>`).join('');
  return `<!doctype html><html lang="en"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>ARGUS PRO local archive</title>
    <style>body{font:1rem system-ui;max-width:52rem;margin:2rem auto;padding:0 1rem}
    article{border-top:1px solid #bbb;padding:1rem 0}article p{white-space:pre-wrap;overflow-wrap:anywhere}</style>
    <main><h1>ARGUS PRO local archive</h1>
    <p id="post-count">${posts.length} archived posts</p>
    ${updatedAt ? `<p>Archive updated <time>${escapeHtml(updatedAt)}</time></p>` : ''}
    <nav><a href="/api/posts">Posts API</a> · <a href="/api/health">Source health</a></nav>
    ${items}</main></html>`;
}

createServer(async (req, res) => {
  function reply(status, type, value) {
    res.writeHead(status, {
      'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
      'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
    });
    res.end(value);
  }
  const json = (status, body) => reply(status, 'application/json; charset=utf-8', JSON.stringify(body));
  try {
    const url = new URL(req.url, 'http://127.0.0.1');
    if (req.method !== 'GET') return json(405, { error: 'method_not_allowed' });
    if (url.pathname === '/api/health') return json(200, await read('local-health.json'));
    if (!['/', '/api/posts', '/api/sources'].includes(url.pathname)) return json(404, { error: 'not_found' });
    const [snapshot, catalog] = await Promise.all([read('local-snapshot.json'), read('sources.json')]);
    const api = createWorkspaceData(snapshot, null, catalog.sources);
    if (url.pathname === '/') {
      const { body } = JSON.parse(api('/api/posts?sort=discovered_at'));
      return reply(200, 'text/html; charset=utf-8', render(body.posts, snapshot.updated_at));
    }
    const { status, body } = JSON.parse(api(url.pathname + url.search));
    return json(status, body);
  } catch (error) {
    console.error('local_read_failed', error.code || error.name);
    return json(503, { error: 'local_archive_unavailable' });
  }
}).listen(Number(port), '127.0.0.1', () => console.log(`http://127.0.0.1:${port}/`));
