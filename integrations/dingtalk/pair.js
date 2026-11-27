'use strict';
const field=id=>document.getElementById(id);
let ticket=null, expiryTimer=null;
function forget(){
  clearTimeout(expiryTimer); ticket=null; field('claim').textContent='';field('proof').value='';field('claim-box').hidden=true;
}
async function post(suffix,body){
  const r=await fetch('/v1/im/dingtalk/'+suffix,{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',cache:'no-store',body:JSON.stringify(body),signal:AbortSignal.timeout(15000)});
  if(!r.ok)throw Error(r.status===401||r.status===403?'请先登录麦米，并确认配对码、会话或绑定仍有效。':'暂未取得确认结果，请稍后查看。');
  return r.json();
}
function bind(id,work){
  field(id).addEventListener('click',async()=>{
    const buttons=[...document.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);
    try{field('result').textContent=await work();}
    catch(e){field('result').textContent=e.name==='TimeoutError'?'未取得结果。':e.message;}
    finally{buttons.forEach(b=>b.disabled=false);}
  });
}
bind('start',async()=>{
  forget();const data=await post('pair/start',{});
  ticket=data.ticket;field('claim').textContent=data.command;field('claim-box').hidden=false;
  expiryTimer=setTimeout(()=>{forget();field('result').textContent='本次配对码已过期。';},data.expires_in_seconds*1000);
  return '已生成一次性配对指令。';
});
bind('confirm',async()=>{
  const proof=field('proof').value.trim();
  if(!ticket||!proof)throw Error('请先领取配对指令并填写确认码。');
  await post('pair/confirm',{ticket,proof});forget();return '钉钉与麦米账号已配对。';
});
bind('consent',async()=>{
  const data=await post('consent',{scope:'dingtalk-private-text-v1'});
  return '文字问答已授权，到期时间 '+new Date(data.expires_at*1000).toLocaleTimeString('zh-CN')+'。';
});
bind('revoke',async()=>{
  await post('revoke',{});forget();return '已解除绑定。';
});
window.addEventListener('pagehide',forget);

