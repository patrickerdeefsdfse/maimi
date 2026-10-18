const test=require('node:test'),assert=require('node:assert/strict');
const {createSessionExpiry}=require('../src/company-auth/auth.cjs');
test('IAM proof renewal extends the same parent session and expiry revokes it',async()=>{
 let now=0,expireCount=0,renewCount=0;const auth={expiresAt:900000,renewal:{instanceId:'synthetic',verifier:'synthetic'}};
 const expiry=createSessionExpiry({now:()=>now,setTimer:()=>1,clearTimer:()=>{},onExpire:a=>{assert.equal(a,auth);expireCount++;},renew:async a=>{assert.equal(a,auth);renewCount++;return now+900000;}});
 expiry.arm(auth);now=800000;await expiry.refresh();assert.equal(auth.expiresAt,1700000);assert.equal(renewCount,1);
 now=1700001;assert.equal(expiry.check(),true);assert.equal(expireCount,1);expiry.clear();
});
test('server revocation stops the active parent session and stale renewals cannot restore it',async()=>{
 let revoked=0,resolveRenew;const auth={expiresAt:Date.now()+900000,renewal:{}};
 const expiry=createSessionExpiry({setTimer:()=>1,clearTimer:()=>{},onExpire:()=>revoked++,renew:async()=>{throw Object.assign(Error('rejected'),{status:401});}});
 expiry.arm(auth);await expiry.refresh();assert.equal(revoked,1);
 const stale=createSessionExpiry({setTimer:()=>1,clearTimer:()=>{},onExpire:()=>{},renew:()=>new Promise(resolve=>resolveRenew=resolve)});
 stale.arm(auth);const original=auth.expiresAt;const pending=stale.refresh();stale.clear();resolveRenew(Date.now()+800000);await pending;assert.equal(auth.expiresAt,original);
});
