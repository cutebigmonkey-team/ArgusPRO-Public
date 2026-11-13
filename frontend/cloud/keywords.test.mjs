import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkspaceData} from './workspace-data.mjs';

test('saved phrase combines with feed filters and searches titles and text literally', () => {
  const items = [
    {id:'title',title:'Ремонт на МІСТ + ДАМБА'},
    {id:'body',text:'міст + дамба'},
    {id:'other',source_name:'міст + дамба'},
    {id:'dsns',origin:'dsns',text:'міст + дамба'},
    {id:'telegram',platform:'telegram',text:'міст + дамба'},
  ].map(p => ({origin:'general',platform:'rss',...p}));
  const response = createWorkspaceData({items});
  const ids = word => JSON.parse(response('/api/posts?origin=general&platform=rss&'+
    new URLSearchParams({keyword:word}))).body.posts.map(p => p.id);
  assert.deepEqual(ids('міст + дамба'),['title','body']);
  assert.deepEqual(ids('повінь'),[]);
  assert.deepEqual(ids(''),['title','body','other']);
});

test('keyword edits survive a new Drive session and deletion removes only that word', async () => {
  const folderId = 'folder_ident_12345';
  globalThis.location = new URL('https://argus.example.org');
  globalThis.window = {isSecureContext:true,ARGUS_CLOUD_CONFIG:{folderId,clientId:'abc.apps.googleusercontent.com'}};
  window.google = globalThis.google = {accounts:{oauth2:{hasGrantedAllScopes:()=>true,
    initTokenClient:options=>({requestAccessToken:()=>options.callback({access_token:'test',expires_in:3600})})}}};
  const files = new Map();
  globalThis.fetch = async (path,options) => {
    const url = new URL(path);
    if (url.pathname.endsWith('/about')) return Response.json({user:{permissionId:'reader'}});
    if (options.method === 'GET') {
      assert.ok(url.searchParams.get('q').includes(`'${folderId}' in parents and trashed=false`));
      return Response.json({files:[...files.values()].filter(f => !f.trashed)});
    }
    const body = JSON.parse(options.body);
    const id = options.method === 'POST' ? 'keyword_ident_'+files.size : url.pathname.split('/').pop();
    if (options.method === 'POST') assert.deepEqual(body.parents,[folderId]);
    else { assert.equal(options.method,'PATCH'); assert.ok(files.has(id)); }
    files.set(id,{...files.get(id),...body,id});
    return Response.json(files.get(id));
  };
  const drive = await import('./drive.mjs');
  await drive.signIn();
  const one = await drive.writeKeyword(null,'Злива');
  const two = await drive.writeKeyword(null,'Ожеледь');
  await drive.writeKeyword(one.id,'Гроза');
  drive.clearSession();
  await drive.signIn();
  assert.deepEqual(await drive.readKeywords(),[{id:one.id,word:'Гроза'},two]);
  await drive.writeKeyword(one.id,null);
  assert.deepEqual(await drive.readKeywords(),[two]);
});
