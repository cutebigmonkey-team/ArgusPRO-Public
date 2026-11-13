import test from 'node:test';
import assert from 'node:assert/strict';
import {createWorkspaceData} from './workspace-data.mjs';

test('confirmed submission survives a new session and appears before the first post',async()=>{
  const folderId='folder_ident_12345';
  globalThis.window={isSecureContext:true,ARGUS_CLOUD_CONFIG:{folderId,clientId:'abc.apps.googleusercontent.com'}};
  globalThis.location=new URL('https://argus.example.org');
  window.google=globalThis.google={accounts:{oauth2:{hasGrantedAllScopes:()=>true,
    initTokenClient:options=>({requestAccessToken:()=>options.callback({access_token:'test',expires_in:3600})})}}};
  const {signIn,clearSession,readSources,submitSource}=await import('./drive.mjs');
  const files=[];
  globalThis.fetch=async (url,options)=>{
    const u=new URL(url);
    if(u.pathname.endsWith('/about')) return Response.json({user:{permissionId:'same-person'}});
    if(options.method==='POST') {
      const file={...JSON.parse(options.body),id:'file_ident_12345',createdTime:'2026-01-01'};
      files.push(file); return Response.json(file);
    }
    return Response.json({files:u.searchParams.get('q').includes('argus-site-source')?files:[]});
  };
  await signIn();
  const row=await submitSource('Канал','https://t.me/s/My_Channel','','general',[]);
  clearSession(); await signIn();
  const rows=await readSources();
  assert.equal(rows[0].id,row.id);
  const reply=JSON.parse(createWorkspaceData({items:[]},null,rows)('/api/sources'));
  assert.equal(reply.body.sources[0].postCountTotal,0);
  assert.equal(reply.body.sources[0].name,'Канал');
  await assert.rejects(submitSource('Дубль','https://t.me/MY_CHANNEL','','general',rows),/вже є/);
  assert.equal(files.length,1);
});
