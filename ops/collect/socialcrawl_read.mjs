import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
const [root, handle] = process.argv.slice(2);
const {callApi} = await import(pathToFileURL(join(root, 'dist/client.js')));
const result = await callApi(
  {apiKey: process.env.SOCIALCRAWL_API_KEY, baseUrl: 'https://www.socialcrawl.dev'},
  {platform: 'threads', resource: 'user/posts', params: {handle, limit: 5}},
);
console.log(JSON.stringify({status: result.status, items: result.json?.data?.items || []}));
