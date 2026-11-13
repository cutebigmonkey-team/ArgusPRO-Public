import test from 'node:test';import assert from 'node:assert/strict';
test('personal TV walls survive sign-in and never enter the shared archive folder',async()=>{
 const folderId='shared_archive_1234';globalThis.location=new URL('https://argus.example');
 globalThis.window={isSecureContext:true,ARGUS_CLOUD_CONFIG:{folderId,clientId:'abc.apps.googleusercontent.com'}};
 window.google=globalThis.google={accounts:{oauth2:{hasGrantedAllScopes:()=>true,
  initTokenClient:o=>({requestAccessToken:()=>o.callback({access_token:'test',expires_in:3600})})}}};
 const files=new Map();globalThis.fetch=async(path,options)=>{
  const url=new URL(path);if(url.pathname.endsWith('/about'))return Response.json({user:{permissionId:'same-account'}});
  if(options.method==='GET'){const q=url.searchParams.get('q');assert.ok(q.includes("'me' in owners"));assert.ok(q.includes("appProperties has"));assert.ok(!q.includes(folderId));return Response.json({files:[...files.values()]});}
  const body=JSON.parse(options.body);assert.equal(body.parents,undefined);const id=options.method==='POST'?'tv_preferences_1234':url.pathname.split('/').pop();
  files.set(id,{...files.get(id),...body,id});return Response.json(files.get(id));
 };
 const drive=await import('./drive.mjs');await drive.signIn();const settings={version:1,layout:4,slots:['sky','dw'],favorites:['sky'],volume:0,presets:[]};
 await drive.saveTvPreferences(settings);drive.clearSession();await drive.signIn();assert.deepEqual(await drive.loadTvPreferences(),settings);
 await drive.saveTvPreferences({...settings,layout:9});assert.equal(files.size,1);assert.equal((await drive.loadTvPreferences()).layout,9);
 files.set('duplicate',{id:'duplicate',description:'{}'});await assert.rejects(drive.saveTvPreferences(settings),/дублікати/);
 drive.clearSession();
});
