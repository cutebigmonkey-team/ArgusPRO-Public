import test from 'node:test';
import assert from 'node:assert/strict';
import {defaults,validatePreferences,formatDate,matchingPosts} from './preferences.mjs';
test('personal preferences reject global options, invalid timezone, XSS avatar, ranges and duplicate tabs',()=>{
  for(const patch of [{role:'owner'},{sources:{}},{theme:'invalid'},{timezone:'fake'},{avatar:'javascript:alert(1)'},{fontSize:500},{pageSize:0},{tabOrder:Array(11).fill('/')}])assert.throws(()=>validatePreferences({...defaults(),...patch}));
  const p=defaults();const valid=validatePreferences(p);valid.tabOrder.reverse();assert.notDeepEqual(p.tabOrder,valid.tabOrder);
});
test('actual date formatting follows timezone and date format across day boundary',()=>{
  const p={...defaults(),dateFormat:'iso'};
  assert.match(formatDate('2026-11-05T23:30:00Z',p),/2026-11-06/);
  assert.match(formatDate('2026-11-05T23:30:00Z',{...p,timezone:'UTC'}),/2026-11-05/);
  assert.equal(formatDate('not-a-date',p),'—');
});
test('notifications combine source and Unicode keyword, exclude samples and require verified critical status',()=>{
  const n={...defaults().notifications,enabled:true,keywords:['ПОВІНЬ'],sources:['one'],priority:'critical'};
  const posts=[{id:'a',text:'Повінь',source_id:'one',critical:true},{id:'b',text:'Повінь',source_id:'two',critical:true},{id:'c',text:'Повінь',source_id:'one'},{id:'d',text:'Повінь',source_id:'one',critical:true,sample:true}];
  assert.deepEqual(matchingPosts(posts,n).map(p=>p.id),['a']);assert.equal(matchingPosts(posts,{...n,enabled:false}).length,0);
});
test('Drive persistence, account isolation, conflict and denied save are real transport outcomes',async()=>{
  const folderId='archive_folder_12345';let account='owner',denied=false,next=0;
  globalThis.location=new URL('https://argus-test.invalid');
  globalThis.window={isSecureContext:true,ARGUS_CLOUD_CONFIG:{folderId,clientId:'abc.apps.googleusercontent.com'}};
  window.google=globalThis.google={accounts:{oauth2:{hasGrantedAllScopes:()=>true,initTokenClient:o=>({requestAccessToken:()=>o.callback({access_token:account,expires_in:3600})})}}};
  const files=new Map();
  globalThis.fetch=async(path,options)=>{
    const u=new URL(path),who=options.headers.Authorization.replace('Bearer ','');
    if(u.pathname.endsWith('/about'))return Response.json({user:{permissionId:who,emailAddress:who+'@example.invalid'}});
    if(u.pathname.endsWith('/'+folderId))return Response.json({owners:[{permissionId:'owner'}],capabilities:{canAddChildren:true}});
    if(options.method==='GET'){assert.match(u.searchParams.get('q'),/'me' in owners/);return Response.json({files:[...files.values()].filter(f=>f.account===who)});}
    if(denied)return Response.json({error:'denied'},{status:403});
    const data=JSON.parse(options.body);assert.equal(data.parents,undefined,'personal preferences must not use the shared archive');
    const id=options.method==='POST'?'personal_file_'+(++next):u.pathname.split('/').pop(),old=files.get(id);
    if(old&&old.account!==who)return Response.json({}, {status:403});
    const f={...old,...data,id,account:who,ownedByMe:true,shared:false,version:String(Number(old?.version||0)+1)};files.set(id,f);return Response.json(f);
  };
  const drive=await import('./drive.mjs');await drive.signIn();const p=defaults();p.name='Owner display';p.theme='dark';
  const first=await drive.savePreferences(p,null);drive.clearSession();await drive.signIn();assert.deepEqual((await drive.readPreferences()).value,p);
  await assert.rejects(drive.savePreferences({...p,theme:'light'},null),/іншій вкладці/);
  denied=true;await assert.rejects(drive.savePreferences({...p,theme:'light'},first.version));assert.equal((await drive.readPreferences()).value.theme,'dark');denied=false;
  drive.clearSession();account='reader';await drive.signIn();assert.deepEqual((await drive.readPreferences()).value,defaults());
  await assert.rejects(drive.submitControl({action:'update',revision:0,patch:{ai:{enabled:false}}}),e=>e.status===403);
  await assert.rejects(drive.readControlState(),e=>e.status===403);
  await drive.savePreferences({...defaults(),name:'Other user'},null);drive.clearSession();account='owner';await drive.signIn();assert.equal((await drive.readPreferences()).value.name,'Owner display');
});
