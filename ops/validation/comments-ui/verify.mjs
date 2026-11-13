import fs from 'node:fs';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
const manifest=JSON.parse(fs.readFileSync('manifest.json','utf8'));
assert.match(manifest.source_tree_sha256,/^[a-f0-9]{64}$/);
assert.equal(crypto.createHash('sha256').update(JSON.stringify(Object.fromEntries(Object.entries(manifest.files).sort()))).digest('hex'),manifest.source_tree_sha256);
for(const [path,sha] of Object.entries(manifest.files)) {
  assert.match(path,/^frontend\/cloud\/[\w.-]+$/);
  assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex'),sha,path);
}
assert.ok(manifest.files['frontend/cloud/comments.test.mjs']);
assert.ok(manifest.files['frontend/cloud/workspace.mjs']);
console.log('Verified frontend snapshot '+manifest.source_tree_sha256);
