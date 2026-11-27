"""History/space view served by A1/A2 without starting an execution worker."""
import secrets
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from .personal_cloud import actor_for

router=APIRouter()
PAGE=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>麦米 · 历史聊天与空间</title><style nonce="NONCE">
body{margin:0;background:#f6f8fb;color:#172432;font:16px/1.6 system-ui,sans-serif}main{max-width:1120px;margin:auto;padding:32px 20px}h1{font-size:26px}section{background:white;padding:20px;border-radius:14px;margin:16px 0;border:1px solid #e1e6ec}button,a.action{border:1px solid #d2dae4;background:white;color:#145f94;padding:8px 14px;border-radius:8px;cursor:pointer}button:hover{background:#eef6fb}.row{display:flex;gap:12px;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #eef1f5}.muted{color:#59697b;font-size:14px}#layout{display:grid;grid-template-columns:310px 1fr;gap:16px}#list button{display:block;width:100%;text-align:left;margin-bottom:8px}.message{padding:16px;border-radius:10px;background:#f3f6fa;margin:12px 0;white-space:pre-wrap;overflow-wrap:anywhere}#status{color:#8a4900}progress{width:100%}#detail h2{font-size:19px}@media(max-width:700px){#layout{display:block}}
</style><main><a href="/">返回麦米</a><h1>历史聊天与空间</h1>
<p class="muted">聊天保留至最后一次实际对话后的 14 天。普通聊天文件保留 24 小时。企业技能文件会在交付后尽快清理，请及时下载；已下载到你电脑的文件由你管理。</p>
<p id="status" role="status"></p><section><h2>聊天文字空间</h2><p id="textUsage">正在读取…</p><progress id="textMeter" max="1" value="0"></progress><p class="muted">文字每人 128 MiB、每段聊天 64 MiB。可以删除已结束的历史聊天释放额度；附件另按文件期限管理。</p></section><section><h2>云端文件空间</h2><p id="usage">正在读取…</p><progress id="meter" max="1" value="0"></progress><p class="muted">这里统计云端托管文件与正在回收的空间，不包括你电脑的本地文件。</p><div id="files"></div><button id="moreFiles" hidden>更多文件</button></section>
<div id="layout"><section><h2>历史聊天</h2><div id="list"></div><button id="more" hidden>更多聊天</button></section><section id="detail"><h2>选择一段聊天</h2><p class="muted">执行服务休息或暂时不可用时，仍可查看已经保存的记录。过期文件不会因查看聊天而恢复。</p><div id="messages"></div><button id="moreMessages" hidden>继续读取</button></section></div></main>
<script nonce="NONCE">
const el=id=>document.getElementById(id),base='/v1/personal-cloud/history/pg';let next=0,fileOffset=0,current=null,messageNext=0,selection=0;
const size=n=>(Number(n||0)/1024/1024).toFixed(1)+' MiB';
async function api(url,options){const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});if(!r.ok)throw Error(r.status===401?'登录已失效，请返回麦米重新登录。':r.status===410?'这项内容已过期并清理。':r.status===409?'聊天还在使用中，请结束会话后重试；执行服务刚停止时请等待两分钟。':'暂时无法读取，请稍后重试。');return r.json();}
function failed(e){el('status').textContent=e.message;}
async function usage(){const v=await api('/v1/storage/usage');el('usage').textContent=size(v.charged_bytes)+' / '+size(v.quota_bytes);el('meter').max=v.quota_bytes;el('meter').value=v.charged_bytes;}
async function textUsage(){const v=await api('/v1/personal-cloud/history/usage');el('textUsage').textContent=size(v.logical_bytes)+' / '+size(v.quota_bytes);el('textMeter').max=v.quota_bytes;el('textMeter').value=v.logical_bytes;}
async function files(){const v=await api('/v1/storage/files?offset='+fileOffset);for(const f of v.files){const row=document.createElement('div');row.className='row';const text=document.createElement('span');text.textContent=f.name+' · '+size(f.bytes??f.expected_bytes)+' · '+(f.expires_at?'到期 '+new Date(f.expires_at).toLocaleString():'待清理');row.append(text);const a=document.createElement('a');a.className='action';a.textContent='下载';a.href='/v1/storage/files/'+encodeURIComponent(f.file_id)+'/download?session_id='+encodeURIComponent(f.session_id);row.append(a);const b=document.createElement('button');b.textContent='删除';b.onclick=async()=>{b.disabled=true;try{await api('/v1/storage/files/'+encodeURIComponent(f.file_id)+'/delete',{method:'POST',headers:{'X-Maimi-Session-ID':f.session_id}});text.textContent=f.name+' · 正在回收';a.remove();await usage();}catch(e){failed(e);b.disabled=false;}};row.append(b);el('files').append(row);}fileOffset+=v.files.length;el('moreFiles').hidden=v.files.length<100;}
async function listing(){const value=await api(base+'?offset='+next);for(const item of value.items){const row=document.createElement('div'),b=document.createElement('button');b.textContent=(item.title||'未命名聊天')+' · '+new Date(item.last_activity_at).toLocaleDateString();b.onclick=()=>{selection++;current=item;messageNext=0;el('messages').replaceChildren();el('detail').querySelector('h2').textContent=item.title||'未命名聊天';messages(selection).catch(failed);};const remove=document.createElement('button');remove.textContent='删除这段聊天';remove.onclick=async()=>{if(!confirm('删除这段历史聊天，并安排清理它的云端文件？此操作不能恢复。'))return;remove.disabled=true;try{await api(base+'/delete?session_id='+encodeURIComponent(item.session_id),{method:'POST'});row.remove();if(current?.session_id===item.session_id){selection++;current=null;el('messages').replaceChildren();el('moreMessages').hidden=true;}await Promise.all([textUsage(),usage()]);}catch(e){failed(e);remove.disabled=false;}};row.append(b,remove);el('list').append(row);}next=value.next;el('more').hidden=next===null;if(!el('list').children.length)el('list').textContent='暂无保留中的聊天。';}
function textContent(content){if(typeof content==='string')return content;if(!Array.isArray(content))return '';return content.map(p=>p.type==='text'?p.text:((p.type==='image'||p.type==='file')?'[附件：'+(p.name||'文件')+'，按文件期限保留]':'')).filter(Boolean).join('\n');}
async function messages(key){const v=await api(base+'?session_id='+encodeURIComponent(current.session_id)+'&offset='+messageNext);if(key!==selection)return;for(const e of v.events){if(!['user/message','assistant/message'].includes(e.type))continue;if(e.type==='user/message'&&e.data?.source?.kind!=='user')continue;const content=textContent(e.type==='user/message'?e.data?.content:e.data?.message?.content);if(!content)continue;const d=document.createElement('div');d.className='message';d.textContent=(e.type==='user/message'?'我':'麦米')+'\n'+content;el('messages').append(d);}messageNext=v.next;el('moreMessages').hidden=messageNext===null;}
el('more').onclick=()=>listing().catch(failed);el('moreFiles').onclick=()=>files().catch(failed);el('moreMessages').onclick=()=>messages(selection).catch(failed);Promise.all([textUsage(),usage(),files(),listing()]).catch(failed);
</script></html>'''

@router.get('/v1/personal-cloud/history/view')
async def page(request:Request):
    await actor_for(request)
    nonce=secrets.token_urlsafe(24)
    page = PAGE.replace('<main>', '<script nonce="NONCE" defer src="/maimi/web/web-idle.js"></script><main>', 1)
    return HTMLResponse(page.replace('NONCE',nonce),headers={'Cache-Control':'no-store',
      'Content-Security-Policy':"default-src 'none'; script-src 'nonce-"+nonce+"'; style-src 'nonce-"+nonce+"'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
      'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
