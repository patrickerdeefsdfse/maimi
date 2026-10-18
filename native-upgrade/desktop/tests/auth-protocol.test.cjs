'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const GATEWAY='https://synthetic-gateway.invalid';
function load(fetchImpl){const mod={exports:{}};const scope={module:mod,exports:mod.exports,require:name=>name==='./deployment.cjs'?{GATEWAY,MODE:'company'}:require(name),
  fetch:fetchImpl,Date,AbortSignal,setTimeout,clearTimeout,Buffer,URL};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/company-auth/auth.cjs'),'utf8'),scope);return mod.exports;}
function device(){return{deviceId:crypto.randomUUID(),instanceId:crypto.randomUUID(),verifier:'p'.repeat(43),token:'mdr_'+'a'.repeat(43),enrollmentId:crypto.randomUUID(),
  expiresAt:Date.now()+86400000,user:{user_no:'SYNTHETIC_A',account_no:'ACCOUNT_A'},pending:{nextToken:'mdr_'+'b'.repeat(43),requestId:crypto.randomUUID()}};}
function response(d,extra={}){return{access_token:'SYNTHETIC_ACCESS',token_type:'Bearer',expires_in:900,renewal_supported:true,resume_supported:true,
  device_id:d.deviceId,device_expires_at:Math.floor(d.expiresAt/1000),user:d.user,...extra};}

test('legacy guarded session discovery rejection becomes truthful unsupported',async()=>{
  for(const status of [401,403,404,405]){const api=load(async(url,options)=>{
    assert.equal(url,GATEWAY+'/v1/desktop/auth/session?capabilities=remembered-login-v1');assert.equal(options.headers.Authorization,undefined);
    return new Response('not implemented',{status});});assert.equal(await api.capabilities(),false);}
});
test('capability network and503 failures do not masquerade as oldserver or erase credentials',async()=>{
  for(const fetch of [async()=>{throw Error('offline');},async()=>Response.json({detail:'unavailable'},{status:503})])
    await assert.rejects(load(fetch).capabilities());
});
test('newlogin binds same PKCE proof and presaved device transaction to exchange',async()=>{
  const d=device(),calls=[],api=load(async(url,options)=>{const body=JSON.parse(options.body);calls.push({url,body});
    if(url.endsWith('/start'))return Response.json({flow_id:'synthetic-flow',browser_url:GATEWAY+'/v1/desktop/auth/authorize',expires_in:300,verification_code:'ABCD-1234'});
    return Response.json(response(d));});
  let browser=0;const auth=await api.signIn(async()=>{browser++;},{device:d});
  assert.equal(browser,1);assert.equal(calls[0].body.remember_device,true);assert.equal(calls[0].body.instance_id,d.instanceId);
  assert.equal(calls[0].body.code_challenge,crypto.createHash('sha256').update(d.verifier).digest('base64url'));
  assert.equal(calls[1].body.resume_token,d.token);assert.equal(calls[1].body.device_id,d.deviceId);assert.equal(calls[1].body.request_id,d.enrollmentId);
  assert.equal(auth.remembered.deviceId,d.deviceId);assert.equal(auth.renewal.verifier,d.verifier);
});
test('resume sends exact7fields to existing renew endpoint and never sends short access as device authority',async()=>{
  const d=device(),api=load(async(url,options)=>{assert.equal(url,GATEWAY+'/v1/desktop/auth/renew');assert.equal(options.headers.Authorization,undefined);
    const body=JSON.parse(options.body);assert.equal(body.kind,'device_resume');
    assert.deepEqual(Object.keys(body).sort(),['code_verifier','device_id','instance_id','kind','next_resume_token','request_id','resume_token']);
    assert.equal(options.redirect,'error');return Response.json(response(d));});
  const auth=await api.resumeDevice(d);assert.equal(auth.user.user_no,'SYNTHETIC_A');assert.equal(auth.renewal.instanceId,d.instanceId);
});
test('resume rejects changedemployee, changedaccount, longaccess and wrongdevice before adoption',async()=>{
  const d=device();for(const extra of [{user:{user_no:'OTHER',account_no:'ACCOUNT_A'}},{user:{user_no:'SYNTHETIC_A',account_no:'OTHER'}},
    {expires_in:901},{device_id:crypto.randomUUID()},{device_expires_at:1}])await assert.rejects(load(async()=>Response.json(response(d,extra))).resumeDevice(d));
});
test('onlyfixedstructuredstaleerror reaches recoverycontrol without arbitraryservertext',async()=>{
  const d=device(),api=load(async()=>Response.json({detail:'desktop_resume_request_stale',password:'SYNTHETIC_PRIVATE'}, {status:409}));
  await assert.rejects(api.resumeDevice(d),error=>error.status===409&&error.code==='desktop_resume_request_stale'&&!String(error).includes('SYNTHETIC_PRIVATE'));
});
test('device logout works without access token and uses proofbound fixedbody',async()=>{
  const d=device(),api=load(async(url,options)=>{assert.equal(url,GATEWAY+'/v1/desktop/auth/logout');assert.equal(options.headers.Authorization,undefined);
    const body=JSON.parse(options.body);assert.equal(body.kind,'device_logout');assert.deepEqual(Object.keys(body).sort(),['code_verifier','device_id','instance_id','kind','resume_token']);
    assert.equal(body.resume_token,d.token);return Response.json({revoked:true});});await api.revokeDevice(d);
});

test('explicit zero device expiry persists while access token remains finite',async()=>{
 const d=device(),api=load(async()=>Response.json(response(d,{device_expires_at:0})));
 const before=Date.now(),auth=await api.resumeDevice(d);
 assert.equal(auth.remembered.expiresAt,0);assert(auth.expiresAt>=before+899000&&auth.expiresAt<=Date.now()+900000);
 for(const value of [-1,'0',null,0.5])await assert.rejects(load(async()=>Response.json(response(d,{device_expires_at:value}))).resumeDevice(d));
});

