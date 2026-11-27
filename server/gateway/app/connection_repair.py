"""Bind the owned company UI to the pinned native Connection API; no auth bypass."""
from __future__ import annotations
import hashlib
import re
import time
from .iam_oauth import IAMFailure
from .order_confirm_client import order_confirm_client
from .skill_picker_client import skill_picker_client
from .badge_result_client import badge_result_client

COMPANY_BYTES = 1827832
COMPANY_SHA256 = '6f432f622a5a6a0b083f61320df6b9f4b19ae690683cdeb6b8aeee2047ae033e'
# R93: preserve old active workers and admit only the newly built exact module.
COMPANY_PINS = ((1827832, '6f432f622a5a6a0b083f61320df6b9f4b19ae690683cdeb6b8aeee2047ae033e'), (1828093, '8a5ee7ab847219d625eb511e398b8f9b652f8487aeee51b3f5aea1f023b60d61'))
# Production r96 removes logistics; bind its exact reviewed company module.
COMPANY_PINS += ((1825466, 'be22577cbc2cdd583499e138bd271fd2d274f3c1a072aa4132cb8cc17641527a'),)
COMPANY_PINS += ((1825566, '4a48a4ca9bde6fde40046289c781887c7f3af5645098f56e564b6fd323dfde05'),)
COMPANY_PINS += ((1826018, '55ada5ce29e97c6d25b720b3282d4bfddf3ecb85c6cb0a1dcaabb025968236ca'),)
COMPANY_PINS += ((1826051, '3de0b1310ee788d1496ab760c0a2402d6428503dba4a4601f4489df91bd370eb'),)
COMPANY_PINS += ((1815823, '577b21197ae2d9f37e738050234759b2e92ba3bd57ddc0fb3813ec485bbfadc4'),)
COMPANY_PINS += ((1816724, 'efb148545c786994008db44293b53b96fda62f153258ed5abce8c60dd6357aad'),)
COMPANY_PINS += ((1816843, 'd045a61c5bac77020a42e00c33ba123f1b12d9cba1a9f6808b040cfdb9f047b2'),)
COMPANY_PINS += ((1832526, '223922ac4abcc6d3b3ae405f62b68ac2347902bd539aed9b8f8e586654ba004d'),)
COMPANY_PINS += ((1833076, '94e261f8b5a10cc4e49d70c415860e89fa102d7ceb96bbf9a62b864eeb27c28a'),)
COMPANY_PREFIX = b'window.__ModuleLoader__.load({\n  id: "@megmeet/dsh-company-ui",\n'
COMPANY_FILE = '@megmeet/dsh-company-ui/client.js'
SETTINGS_FILE = '@deepseek-ai/dsh-client-ui-settings-general/client.js'
SETTINGS_PREFIX = b'\nwindow.__ModuleLoader__.load({id:`@deepseek-ai/dsh-client-ui-settings-general`,'
# Both the single-module and rc.5 combo routes retain this identical prefix
# through the last JavaScript newline, before their different source-map trailers.
SETTINGS_BYTES = 89920
SETTINGS_SHA256 = 'e857839f5576b9321d00e9044b934a42e8976f3c17701cb5dab3998ea94a25a8'
APPLY_ANCHOR = b'return { inject: ["slots"], apply(ctx) {\n'
APPLY_INSERT = (b'      if (typeof globalThis.__MEGMEET_ATTACH_CONNECTION_R90__ === "function") '
                b'globalThis.__MEGMEET_ATTACH_CONNECTION_R90__(ctx);\n')
SESSION_SHA256 = '5c50c61d50d773403ace6af85d42ee902e50fb0bafee93fca7d115e88dff0431'
MAX_BUNDLE_BYTES = 16 * 1024 * 1024
COMBO = re.compile(r'\?(?P<files>(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+/client\.js'
                   r'(?:,(?:@[a-z0-9_.-]+/)?[a-z0-9_.-]+/client\.js)*)&rev=[a-f0-9]{12}\Z')

BINDING_SCRIPT = r'''
// r90: only our byte-pinned company module calls this adapter with its own ctx.
(function installNativeConnectionBinding(target) {
  'use strict';
  if (target.__MEGMEET_ATTACH_CONNECTION_R90__) return;
  const attached = new WeakSet();
  target.__MEGMEET_ATTACH_CONNECTION_R90__ = function attach(ctx) {
    const recovery = target.__MEGMEET_CLOUD_RECOVERY__;
    if (!recovery || !ctx || typeof ctx.inject !== 'function') {
      recovery?.adapterUnavailable(); return;
    }
    if (attached.has(ctx)) return;
    attached.add(ctx);
    ctx.inject(['connection'], scoped => {
      const connection = scoped.get('connection');
      if (!connection || typeof connection.state?.getSnapshot !== 'function'
          || typeof connection.state?.subscribe !== 'function'
          || typeof connection.reconnect !== 'function') {
        recovery.adapterUnavailable(); return;
      }
      const release = recovery.attachNative({
        getState: () => connection.state.getSnapshot(),
        subscribe: listener => connection.state.subscribe(listener),
        reconnect: () => connection.reconnect(),
        // No page reload is authorized by this adapter, even for an idle page.
        canRefresh: () => false,
      });
      scoped.on('dispose', () => { release(); recovery.adapterUnavailable(); });
    });
  };
})(globalThis);
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError('R90_SESSION_ANCHOR_CHANGED')
    return text.replace(old, new, 1)


def adapt_session_script(original):
    if hashlib.sha256(original.encode('utf8')).hexdigest() != SESSION_SHA256:
        raise RuntimeError('R90_SESSION_SOURCE_CHANGED')
    start = original.index('    function maybeRefresh() {')
    end = original.index("    async function recover(reason='manual') {", start)
    text = original[:start] + '''    function maybeRefresh() {
      refreshTimer=null;
      if(!active())return;
      if(!hooks){publish({status:'transport_unavailable'});return;}
      if(hooks.getState()==='connected'){publish({status:'ready'});return;}
      // The native loop owns transport retry. Never infer disposable drafts or reload.
      publish({status:'refresh_waiting'});
    }
''' + original[end:]
    text = replace_once(text,
        "            // Never replay the failed business request. Only replace transport.\n            queueRequest=null;queueDeadline=0;\n            hooks?.reconnect();",
        """            queueRequest=null;queueDeadline=0;
            // Host readiness is separate from the native event generation.
            if(!hooks){publish({status:'transport_unavailable'});return false;}
            if(hooks.getState()==='connected'){publish({status:'ready'});void presence();return true;}
            // Never replay the failed business request. Only replace transport.
            hooks.reconnect();""")
    text = replace_once(text, "      if(state.status==='recovering')hooks.reconnect();\n", '')
    text = replace_once(text, '      hasNative:()=>!!hooks,',
        "      hasNative:()=>!!hooks,\n      adapterUnavailable:()=>{if(active()&&!hooks)publish({status:'transport_unavailable'});},")
    text = replace_once(text,
        "      refresh_waiting:'连接仍未恢复。请先处理当前草稿、附件或任务，再刷新页面。',",
        "      refresh_waiting:'连接暂未恢复。当前输入已保留，可重新连接。',\n      transport_unavailable:'页面连接组件尚未就绪。当前输入已保留，请稍后重新连接。',")
    text = replace_once(text,
        "    }else if(['recovery_failed','refresh_waiting','idle_released','queue_expired','queue_cancelled','queue_cancel_unconfirmed'].includes(snapshot.status)){",
        "    }else if(['recovery_failed','refresh_waiting','transport_unavailable','idle_released','queue_expired','queue_cancelled','queue_cancel_unconfirmed'].includes(snapshot.status)){")
    if 'options.refresh()' in text:
        raise RuntimeError('R90_AUTOMATIC_REFRESH_REMAINS')
    return text + BINDING_SCRIPT


def connection_bundle_plan(method, path, query, headers):
    if method != 'GET':
        return False
    selected = frozenset()
    if path == '/plugins/' and len(query) <= 4096:
        match = COMBO.fullmatch(query)
        if match:
            files = match.group('files').split(',')
            selected = frozenset(files).intersection((COMPANY_FILE, SETTINGS_FILE))
            if selected and (len(files) > 100 or len(set(files)) != len(files)):
                raise IAMFailure(404, 'personal_cloud_connection_asset_invalid')
    elif path in ('/plugins/' + COMPANY_FILE, '/plugins/' + SETTINGS_FILE):
        if re.fullmatch(r'rev=[a-f0-9]{12}', query):
            selected = frozenset((path.removeprefix('/plugins/'),))
    if selected and headers.get('range'):
        raise IAMFailure(404, 'personal_cloud_connection_asset_invalid')
    return selected


SALES_TIMELINE_PATCH = [(1741127, 1741127, b"  const salesActions = new Set(['status','recognize','review','lookup_customer','lookup_price','generate_po','revise','prepare','query']);\n"), (1741359, 1741513, b"    // Manual submit/environment aliases are not model operations. Their only\n    // entry is an authorized result card and the existing human invoke route.\n    return (args?.connector==='service'&&serviceActions.has(args.action)||args?.connector==='sales-orders'&&salesActions.has(args.action))&&args.arguments&&typeof args.arguments==='object'&&!Array.isArray(args.arguments)\n"), (1741592, 1741629, b'  function cardResult(content,seq,connector) {\n'), (1741966, 1742060, b"      if(!uuid.test(value?.job_id||'')||!projectUI.safeCard(card)||card.connector!==connector\n"), (1742171, 1742377, b"      if(connector==='service'&&card.id==='service-connection'&&value.service_result?.protocol!=='maimi-service-session-v2')return null;\n      return {jobIds:[value.job_id.toLowerCase()],service:connector==='service',salesOrder:connector==='sales-orders',expectedConnector:connector,expectedCardId:card.id,seq};\n"), (1743173, 1743220, b'    return cardResult(data.content,event.seq,data.arguments.connector);\n'), (1743662, 1743859, b'    try{const args=JSON.parse(raw);return serviceArguments(args)?{callId:event.data.callId,callSeq:event.seq,turn:event.data.turn,step:event.data.step,connector:args.connector,action:args.action}:null;}catch{return null;}\n'), (1744359, 1744464, b'    const parsed=cardResult(message.content,event.seq,bound.connector);return parsed?{...parsed,serviceCall:bound}:null;\n'), (1746413, 1746469, b"      return {jobIds,salesOrder,expectedConnector:salesOrder?'sales-orders':undefined,seq:match.event.seq};},\n"), (1747268, 1747339, b'  function ProjectJob({sessionId,jobId,service=false,expectedConnector,expectedCardId}){\n'), (1747744, 1747909, b"    const safeRows=value=>(Array.isArray(value?.history)?value.history:[]).filter(row=>uuid.test(row?.job_id||'')&&projectUI.safeCard(row?.result?.project_result)&&(!expectedConnector||row.result.project_result.connector===expectedConnector));\n"), (1749649, 1749860, b"          if(expectedConnector?(row.result.project_result.connector!==expectedConnector||(expectedCardId&&row.result.project_result.id!==expectedCardId)):service?(row.result.project_result.connector!=='service'||row.result.project_result.id!==expectedCardId):row.result.project_result.connector==='service')throw Error('PROJECT_ROOT_BINDING_INVALID');\n"), (1751643, 1751709, b'    },[sessionId,jobId,service,expectedConnector,expectedCardId,revision,pending]);\n'), (1755239, 1755431, b"      node.data.jobIds.filter(id=>uuid.test(id)).map(jobId=>h(ProjectJob,{key:sessionId+':'+jobId,sessionId,jobId,service:node.data.service===true,expectedConnector:node.data.expectedConnector,expectedCardId:node.data.expectedCardId})));\n")]
SALES_EDITOR_PATCH = [(1754037, 1754308, b"    const ownerByOrder=new Map();\n    for(const r of [root,...children].filter(Boolean)){\n      const c=r.result?.project_result;\n      if(c?.connector==='sales-orders')for(const id of projectUI.salesRefs(c))ownerByOrder.set(id,r.job_id);\n    }\n    const render=row=>h(projectUI.ProjectCard,{key:row.job_id,card:row.result.project_result,sessionId,historyJobId:row.job_id,parentJobId:jobId,activeOrders:new Set([...ownerByOrder].filter(([,job])=>job===row.job_id).map(([id])=>id)),onRefresh:refreshed,pendingOperation:!!pending,readOnly:!!pending||children.length>0&&row.job_id!==children.at(-1)?.job_id});\n"), (1805928, 1806043, b"  const orderFields = [\n    ['business_entity','\xe9\x94\x80\xe5\x94\xae\xe5\x85\xac\xe5\x8f\xb8'],['customer_name','\xe5\xae\xa2\xe6\x88\xb7\xe5\x90\x8d\xe7\xa7\xb0'],['customer_po','\xe5\xae\xa2\xe6\x88\xb7\xe9\x87\x87\xe8\xb4\xad\xe5\x8d\x95\xe5\x8f\xb7'],\n    ['currency','\xe5\xb8\x81\xe7\xa7\x8d'],['account_number','\xe5\xae\xa2\xe6\x88\xb7\xe8\xb4\xa6\xe5\x8f\xb7'],['order_type','\xe8\xae\xa2\xe5\x8d\x95\xe7\xb1\xbb\xe5\x9e\x8b'],\n    ['price_list_name','\xe4\xbb\xb7\xe7\x9b\xae\xe8\xa1\xa8'],['order_date','\xe8\xae\xa2\xe5\x8d\x95\xe6\x97\xa5\xe6\x9c\x9f'],['total_amount','\xe8\xae\xa2\xe5\x8d\x95\xe6\x80\xbb\xe9\xa2\x9d'],\n    ['total_basis','\xe6\x80\xbb\xe9\xa2\x9d\xe5\x8f\xa3\xe5\xbe\x84'],['salesrep_name','\xe4\xb8\x9a\xe5\x8a\xa1\xe5\x91\x98'],['head_remark','\xe8\xae\xa2\xe5\x8d\x95\xe5\xa4\x87\xe6\xb3\xa8']\n  ];\n  const itemFields = [\n    ['item_code','\xe7\x89\xa9\xe6\x96\x99\xe7\xbc\x96\xe7\xa0\x81'],['customer_item','\xe5\xae\xa2\xe6\x88\xb7\xe6\x96\x99\xe5\x8f\xb7'],['quantity','\xe6\x95\xb0\xe9\x87\x8f'],['unit','\xe5\x8d\x95\xe4\xbd\x8d'],\n    ['unit_price','\xe5\x8d\x95\xe4\xbb\xb7'],['price_basis','\xe5\x8d\x95\xe4\xbb\xb7\xe5\x8f\xa3\xe5\xbe\x84'],['request_date','\xe4\xba\xa4\xe8\xb4\xa7\xe6\x97\xa5\xe6\x9c\x9f'],\n    ['tax_rate','\xe7\xa8\x8e\xe7\x8e\x87\xef\xbc\x880\xe2\x80\x931\xef\xbc\x89'],['description','\xe7\x89\xa9\xe6\x96\x99\xe5\x90\x8d\xe7\xa7\xb0'],['specification','\xe8\xa7\x84\xe6\xa0\xbc'],\n    ['amount','\xe8\xa1\x8c\xe9\x87\x91\xe9\xa2\x9d'],['amount_basis','\xe9\x87\x91\xe9\xa2\x9d\xe5\x8f\xa3\xe5\xbe\x84'],['source_line','\xe5\x8e\x9f\xe5\x8d\x95\xe8\xa1\x8c\xe5\x8f\xb7'],['remark','\xe8\xa1\x8c\xe5\xa4\x87\xe6\xb3\xa8']\n  ];\n  const basisFields=new Set(['price_basis','amount_basis','total_basis']);\n  const numericFields=new Set(['quantity','unit_price','amount','tax_rate','total_amount']);\n  const validEditor=editor=>editor?.schema==='sales-order-editor-v1'&&opaque(editor.order_id)&&\n    Number.isSafeInteger(editor.revision)&&editor.revision>0&&editor.header&&typeof editor.header==='object'&&\n    !Array.isArray(editor.header)&&Array.isArray(editor.lines)&&editor.lines.length<=500&&\n    editor.lines.every(row=>row&&typeof row==='object'&&!Array.isArray(row));\n  const salesRefs=card=>new Set([\n    ...(Array.isArray(card?.order_editors)?card.order_editors.filter(validEditor).map(x=>x.order_id):[]),\n    ...(Array.isArray(card?.sales_order_refs)?card.sales_order_refs.filter(opaque):[]),\n    ...(Array.isArray(card?.actions)?card.actions.map(a=>a?.arguments?.order_id).filter(opaque):[])\n  ]);\n  const flagTone={\n    '\xe5\xbe\x85\xe8\xa1\xa5': ['#883728','#fce8e3'], '\xe5\xbe\x85\xe6\xa0\xb8': ['#734a09','#fff0c9'],\n    '\xe8\xa7\x84\xe5\x88\x99\xe9\xbb\x98\xe8\xae\xa4': ['#305970','#e3f1f8'], '\xe5\x8e\x9f\xe5\x80\xbc': ['#3a5948','#eaf1eb'],\n    '\xe5\xb7\xb2\xe4\xbf\xae\xe6\x94\xb9': ['#27587e','#deedf9'], '\xe9\x80\x89\xe5\xa1\xab': ['#5a605c','#f0f1ee']\n  };\n  const plain=v=>v==null?'':String(v);\n  const own=(o,k)=>o!=null&&Object.prototype.hasOwnProperty.call(o,k);\n  const nullable=v=>v===''?null:v;\n  function SalesEditor({editor,card,sessionId,parentJobId,readOnly,superseded,actions,onRefresh,onDirty}) {\n    const {order_id:id,revision,header,lines}=editor;\n    const [layout,setLayout]=useState(()=>lines.map((_,i)=>({key:'s'+i,source:i})));\n    const [changes,setChanges]=useState({}),[busy,setBusy]=useState(false),[notice,setNotice]=useState(''),[page,setPage]=useState(0),[extra,setExtra]=useState(false);\n    const next=useRef(0),lock=useRef(false);\n    useEffect(()=>{\n      if(superseded){setLayout(lines.map((_,i)=>({key:'s'+i,source:i})));setChanges({});setNotice('');}\n    },[superseded]);\n    const changedLayout=layout.length!==lines.length||layout.some((row,i)=>row.source!==i);\n    const dirty=changedLayout||Object.keys(changes).length>0, inactive=readOnly||busy;\n    useEffect(()=>{onDirty(id,dirty);return()=>onDirty(id,false);},[id,dirty]);\n    const bind=(path,value,initial)=>{\n      setChanges(old=>{const next={...old};if(value===initial)delete next[path];else next[path]=value;return next;});\n      setNotice('');\n    };\n    const clamp=p=>Math.max(0,Math.min(p,Math.max(0,Math.ceil(layout.length/12)-1)));\n    const pageIndex=clamp(page);\n    const issues=Array.isArray(editor.issues)?editor.issues:[];\n    const indexed=React.useMemo(()=>{\n      const byPath=new Map();\n      for(const issue of issues){if(typeof issue?.path!=='string')continue;const list=byPath.get(issue.path)||[];list.push(issue);byPath.set(issue.path,list);}\n      return byPath;\n    },[editor.issues]);\n    const defaults=editor.business_defaults||editor.defaults||{};\n    const cell=(field,label,row=null,displayIndex=null)=>{\n      const local=row?row.key+'.'+field:'header.'+field;\n      const src=row?(row.source==null?null:'lines.'+row.source+'.'+field):field;\n      const base=plain(row?(row.source==null?null:lines[row.source][field]):header[field]);\n      const value=own(changes,local)?changes[local]:base;\n      const issue=src===null?[]:(indexed.get(src)||[]);\n      const rule=src!==null&&own(defaults,src)?defaults[src]:null;\n      const confidence=src!==null?editor.confidence?.[src]:null;\n      const required=row?['item_code','quantity','unit_price','request_date'].includes(field):['business_entity','customer_name','customer_po','currency','account_number'].includes(field);\n      const modified=own(changes,local),absent=value.trim()==='';\n      const flag=modified?'\xe5\xb7\xb2\xe4\xbf\xae\xe6\x94\xb9':(absent&&(required||issue.some(x=>x.code==='required')))?'\xe5\xbe\x85\xe8\xa1\xa5':\n        issue.some(x=>x.severity==='error')?'\xe5\xbe\x85\xe6\xa0\xb8':rule?'\xe8\xa7\x84\xe5\x88\x99\xe9\xbb\x98\xe8\xae\xa4':\n        issue.length||typeof confidence==='number'&&confidence<0.8||value==='unknown'?'\xe5\xbe\x85\xe6\xa0\xb8':absent?'\xe9\x80\x89\xe5\xa1\xab':'\xe5\x8e\x9f\xe5\x80\xbc';\n      const [color,background]=flagTone[flag];\n      const observation=src!==null?editor.observations?.[src]:null;\n      const description=[modified?'\xe4\xbf\x9d\xe5\xad\x98\xe5\x89\x8d\xef\xbc\x9a'+(base||'\xe7\xa9\xba'):'',rule?((rule.previous!=null?'\xe5\x8e\x9f\xe5\x80\xbc\xef\xbc\x9a'+plain(rule.previous)+'\xe3\x80\x82':'')+plain(rule.reason||'\xe6\x8c\x89\xe4\xb8\x9a\xe5\x8a\xa1\xe8\xa7\x84\xe5\x88\x99\xe5\xa1\xab\xe5\x85\xa5')):'',\n        observation?'\xe5\x8e\x9f\xe8\xaf\x86\xe5\x88\xab\xef\xbc\x9a'+text(observation,300):'',...issue.map(x=>text(x.message,260))].filter(Boolean).join(' ');\n      const name=(row?'\xe7\xac\xac '+(displayIndex+1)+' \xe8\xa1\x8c ':'')+label;\n      const props={'aria-label':name,value,disabled:inactive,onChange:e=>bind(local,e.target.value,base),\n        style:{...control,width:'100%',minWidth:0,boxSizing:'border-box',borderRadius:5,background:'var(--dsw-alias-bg-base,#fff)',padding:'5px 7px',cursor:inactive?'default':'text'}};\n      const controlNode=basisFields.has(field)?h('select',props,...[['','\xe6\x9c\xaa\xe5\xa1\xab\xe5\x86\x99'],['inclusive','\xe5\x90\xab\xe7\xa8\x8e'],['exclusive','\xe6\x9c\xaa\xe7\xa8\x8e'],['unknown','\xe5\xbe\x85\xe7\xa1\xae\xe8\xae\xa4']].map(([v,l])=>h('option',{key:v,value:v},l))):\n        h('input',{...props,type:'text',inputMode:numericFields.has(field)?'decimal':undefined,maxLength:500,autoComplete:'off',spellCheck:false,\n          placeholder:['request_date','order_date'].includes(field)?'YYYY-MM-DD':undefined});\n      return h('label',{key:field,style:{display:'grid',gap:4,minWidth:0,alignContent:'start'}},\n        !row?h('span',{style:{fontWeight:600}},label):null,controlNode,\n        h('span',{title:description||undefined,style:{fontSize:11,lineHeight:1.5,padding:'1px 5px',justifySelf:'start',borderRadius:4,color,background}},flag),\n        description?h('small',{style:{...tone,fontSize:11,maxWidth:210,overflowWrap:'anywhere'}},description):null);\n    };\n    function buildChanges(){\n      const result={};\n      if(changedLayout)result.line_layout=layout.map(row=>row.source);\n      for(const [field] of orderFields){const key='header.'+field;if(own(changes,key))result[field]=nullable(changes[key]);}\n      layout.forEach((row,i)=>{for(const [field] of itemFields){const key=row.key+'.'+field;if(own(changes,key))result['lines.'+i+'.'+field]=nullable(changes[key]);}});\n      return result;\n    }\n    async function invoke(action,args){\n      if(inactive||lock.current||!sid(sessionId)||card.connector!=='sales-orders'||!opaque(id)||typeof handlers.invoke!=='function')return;\n      lock.current=true;setBusy(true);setNotice('');\n      try{\n        const value=await handlers.invoke({sessionId,cardId:card.id,connector:'sales-orders',action,arguments:args,parentJobId});\n        await onRefresh?.(value);\n      }catch(error){setNotice(failureText(error));}\n      finally{lock.current=false;setBusy(false);}\n    }\n    const save=()=>{if(dirty)void invoke('revise',{order_id:id,revision,changes:buildChanges(),reason:'\xe5\x91\x98\xe5\xb7\xa5\xe5\x9c\xa8\xe5\xa4\x8d\xe6\xa0\xb8\xe8\xa1\xa8\xe6\x9b\xb4\xe6\xad\xa3'});};\n    const remove=(r)=>{setLayout(old=>old.filter(it=>it.key!==r.key));setChanges(old=>Object.fromEntries(Object.entries(old).filter(([key])=>!key.startsWith(r.key+'.'))));};\n    const add=()=>{const r={key:'n'+next.current++,source:null};setLayout(old=>[...old,r]);setPage(Math.floor(layout.length/12));};\n    const btn=(label,fn,disabled=false,primary=false)=>h('button',{type:'button',disabled,onClick:fn,\n      style:{...control,borderRadius:7,padding:'6px 10px',...(primary?{background:'#2e6552',color:'#fff',borderColor:'transparent'}:{}),\n        ...(disabled?{opacity:0.55,cursor:'default'}:{})}},label);\n    const grid={display:'grid',gridTemplateColumns:'repeat(auto-fit,minmax(min(155px,100%),1fr))',gap:'10px 14px',margin:'10px 0'};\n    const prepare=actions.find(a=>a?.kind==='invoke'&&a.connector==='sales-orders'&&a.action==='prepare'&&a.arguments?.order_id===id&&a.arguments.revision===revision);\n    const submit=actions.find(a=>a?.kind==='invoke'&&a.connector==='sales-orders'&&a.action==='submit'&&a.arguments?.order_id===id&&a.arguments.revision===revision);\n    const shown=extra?itemFields:itemFields.slice(0,7);\n    const stop=e=>e.stopPropagation();\n    return h('section',{'data-megmeet-sales-editor':id,'aria-label':'\xe9\x94\x80\xe5\x94\xae\xe8\xae\xa2\xe5\x8d\x95\xe5\xa4\x8d\xe6\xa0\xb8\xe8\xa1\xa8',contentEditable:false,onKeyDown:stop,onKeyUp:stop,onPaste:stop,\n      style:{marginTop:14,minWidth:0,borderTop:'1px solid var(--dsw-alias-border-l2,#dce5dc)',paddingTop:12}},\n      h('div',{style:{display:'flex',alignItems:'baseline',gap:9,flexWrap:'wrap'}},h('strong',null,plain(header.customer_po)||'\xe9\x94\x80\xe5\x94\xae\xe8\xae\xa2\xe5\x8d\x95\xe8\x8d\x89\xe7\xa8\xbf'),h('small',{style:tone},'\xe7\x89\x88\xe6\x9c\xac '+revision)),\n      h('div',{style:grid},...orderFields.slice(0,6).map(([k,l])=>cell(k,l))),\n      h('details',null,h('summary',{style:{cursor:'pointer'}},'\xe5\x85\xb6\xe4\xbb\x96\xe8\xae\xa2\xe5\x8d\x95\xe5\xad\x97\xe6\xae\xb5'),h('div',{style:grid},...orderFields.slice(6).map(([k,l])=>cell(k,l)))),\n      h('div',{style:{display:'flex',alignItems:'center',gap:10,flexWrap:'wrap',margin:'14px 0 6px'}},h('strong',null,'\xe8\xae\xa2\xe5\x8d\x95\xe6\x98\x8e\xe7\xbb\x86 \xc2\xb7 '+layout.length+' \xe8\xa1\x8c'),btn(extra?'\xe6\x94\xb6\xe8\xb5\xb7\xe6\x9b\xb4\xe5\xa4\x9a\xe5\x88\x97':'\xe6\x9b\xb4\xe5\xa4\x9a\xe5\x88\x97',()=>setExtra(v=>!v))),\n      h('div',{tabIndex:0,'aria-label':'\xe8\xae\xa2\xe5\x8d\x95\xe6\x98\x8e\xe7\xbb\x86\xe8\xa1\xa8',style:{overflowX:'auto',border:'1px solid var(--dsw-alias-border-l2,#dce5dc)',borderRadius:8}},\n        h('table',{style:{borderCollapse:'collapse',width:'100%',fontSize:12,textAlign:'left'}},\n          h('thead',null,h('tr',null,...['\xe8\xa1\x8c\xe5\x8f\xb7',...shown.map(it=>it[1]),...(readOnly?[]:['\xe6\x93\x8d\xe4\xbd\x9c'])].map(l=>h('th',{key:l,scope:'col',style:{padding:'8px 7px',whiteSpace:'nowrap',background:'var(--dsw-alias-interactive-bg-hover,#eff4ef)'}},l)))),\n          h('tbody',null,...layout.slice(pageIndex*12,pageIndex*12+12).map((r,j)=>{\n            const i=pageIndex*12+j;\n            return h('tr',{key:r.key},h('th',{scope:'row',style:{padding:7,verticalAlign:'top'}},String(i+1)),...shown.map(([k,l])=>\n              h('td',{key:k,style:{minWidth:['item_code','customer_item','request_date'].includes(k)?128:85,padding:5,verticalAlign:'top',borderTop:'1px solid var(--dsw-alias-border-l2,#e2e8e1)'}},cell(k,l,r,i))),\n              !readOnly?h('td',{style:{verticalAlign:'top',padding:5}},btn('\xe5\x88\xa0\xe9\x99\xa4\xe7\xac\xac '+(i+1)+' \xe8\xa1\x8c',()=>remove(r),inactive)):null);\n          })))),\n      h('div',{style:{display:'flex',gap:8,alignItems:'center',marginTop:7}},!readOnly?btn('\xe6\xb7\xbb\xe5\x8a\xa0\xe6\x98\x8e\xe7\xbb\x86\xe8\xa1\x8c',add,inactive||layout.length>=500):null,\n        layout.length>12?h(React.Fragment,null,btn('\xe4\xb8\x8a\xe4\xb8\x80\xe9\xa1\xb5',()=>setPage(pageIndex-1),!pageIndex),\n          h('span',{style:tone},(pageIndex+1)+' / '+Math.ceil(layout.length/12)),btn('\xe4\xb8\x8b\xe4\xb8\x80\xe9\xa1\xb5',()=>setPage(pageIndex+1),pageIndex>=Math.ceil(layout.length/12)-1)):null),\n      h('div',{style:{display:'flex',gap:8,alignItems:'center',flexWrap:'wrap',margin:'12px 0 4px'}},\n        !readOnly?btn('\xe4\xbf\x9d\xe5\xad\x98\xe6\x9b\xb4\xe6\xad\xa3',save,inactive||!dirty,true):null,\n        !readOnly&&dirty?btn('\xe6\x92\xa4\xe9\x94\x80\xe6\x9c\xac\xe6\xac\xa1\xe4\xbf\xae\xe6\x94\xb9',()=>{setLayout(lines.map((_,i)=>({key:'s'+i,source:i})));setChanges({});setNotice('');},inactive):null,\n        !readOnly&&prepare?btn('\xe9\xa2\x84\xe8\xa7\x88\xe8\xae\xa2\xe5\x8d\x95',()=>invoke('prepare',prepare.arguments),inactive||dirty):null,\n        !readOnly&&submit?btn('\xe7\xa1\xae\xe8\xae\xa4\xe5\x88\x9b\xe5\xbb\xba Oracle \xe8\xae\xa2\xe5\x8d\x95',()=>invoke('submit',submit.arguments),inactive||dirty,true):null,\n        !readOnly?btn('\xe8\xaf\xbb\xe5\x8f\x96\xe6\x9c\x80\xe6\x96\xb0\xe8\x8d\x89\xe7\xa8\xbf',()=>invoke('review',{order_id:id}),inactive):null,\n        dirty&&!readOnly?h('small',{role:'status',style:tone},'\xe6\x9c\x89\xe6\x9c\xaa\xe4\xbf\x9d\xe5\xad\x98\xe7\x9a\x84\xe6\x9b\xb4\xe6\xad\xa3'):null),\n      notice?h('p',{role:'alert'},notice):null);\n  }\n\n  function ProjectCard({card,sessionId,onRefresh,historyJobId,parentJobId,readOnly=false,pendingOperation=false,activeOrders=null}){\n"), (1806145, 1806145, b'    const dirtyOrders=useRef(new Set());\n    const dirty=(id,flag)=>{if(flag)dirtyOrders.current.add(id);else dirtyOrders.current.delete(id);};\n'), (1806181, 1806515, b"    const editors=card.connector==='sales-orders'&&Array.isArray(card.order_editors)?card.order_editors.filter(validEditor):[];\n    const editorIds=new Set(editors.map(e=>e.order_id));\n    const allActions=Array.isArray(card.actions)?card.actions:[];\n    const editable=id=>!pendingOperation&&(activeOrders instanceof Set?activeOrders.has(id):!readOnly);\n    const availableActions=allActions.filter(a=>!(editorIds.has(a?.arguments?.order_id)&&['prepare','submit'].includes(a?.action)));\n    const run=async action=>{if(busy||readOnly)return;if(dirtyOrders.current.has(action.arguments?.order_id)){setError('\xe8\xaf\xb7\xe5\x85\x88\xe4\xbf\x9d\xe5\xad\x98\xe6\x88\x96\xe6\x92\xa4\xe9\x94\x80\xe6\x9c\xac\xe6\xac\xa1\xe6\x9b\xb4\xe6\xad\xa3\xe3\x80\x82');return;}setBusy(true);setError('');try{const result=await handlers.invoke({sessionId,cardId:card.id,connector:card.connector,action:action.action,arguments:action.arguments||{},parentJobId});await onRefresh?.(result);}catch(error){setError(failureText(error));}finally{setBusy(false);}};\n"), (1808849, 1808849, b"      ...editors.map(e=>h(SalesEditor,{key:sessionId+':'+e.order_id+':'+e.revision,editor:e,card,sessionId,parentJobId,actions:allActions,onRefresh,onDirty:dirty,readOnly:!editable(e.order_id),superseded:activeOrders instanceof Set&&!activeOrders.has(e.order_id)})),\n"), (1810430, 1811057, b"        ...(readOnly?[]:availableActions).slice(0,12).filter(action=>action&&action.connector===card.connector&&!(card.connector==='service'&&action.form==='service-login')).map(action=>h('button',{key:action.id,type:'button',className:'mm-soft-button',disabled:busy||(action.kind==='invoke'?typeof handlers.invoke!=='function':action.kind==='secure_form'?typeof handlers.secureLogin!=='function':isCostForm(action,card)?typeof handlers.invoke!=='function':true),onClick:()=>['secure_form','project_form'].includes(action.kind)?setForm(action):run(action),style:control},text(action.label,100)))),\n"), (1811648, 1811756, b'  return {ProjectCard,Section,salesRefs,ServiceConnection,SecureForm,CostEditForm,costEditPlan,failureText,safeCard};\n')]


SALES_APPROVAL_PATCH = [(1692960, 1693113, b"  const workflowNames = { meeting: '\xe4\xbc\x9a\xe8\xae\xae\xe6\x96\x87\xe6\xa1\xa3', heatcost: '\xe5\x9b\xbe\xe7\xba\xb8\xe6\x88\x90\xe6\x9c\xac\xe5\x88\x86\xe6\x9e\x90', 'infrastructure-monitor': 'vmware', 'sales-orders': '\xe9\x94\x80\xe5\x94\xae\xe8\xae\xa2\xe5\x8d\x95', 'sales-order-approval': '\xe9\x94\x80\xe5\x94\xae\xe8\xae\xa2\xe5\x8d\x95\xe5\xae\xa1\xe6\xa0\xb8' };\n"), (1741655, 1741876, b"    return (args?.connector==='service'&&serviceActions.has(args.action)||args?.connector==='sales-orders'&&salesActions.has(args.action)||args?.connector==='sales-order-approval'&&['pending','detail'].includes(args.action))&&args.arguments&&typeof args.arguments==='object'&&!Array.isArray(args.arguments)\n"), (1742681, 1742856, b"      return {jobIds:[value.job_id.toLowerCase()],service:connector==='service',salesOrder:['sales-orders','sales-order-approval'].includes(connector),expectedConnector:connector,expectedCardId:card.id,seq};\n"), (1746958, 1747068, b"      const approval=jobIds.length>0&&match.event.data?.message?.content?.some(part=>part?.type==='text'&&typeof part.text==='string'&&\n        /(?:^|\\n\\n)\xe9\x94\x80\xe5\x94\xae\xe8\xae\xa2\xe5\x8d\x95\xe5\xae\xa1\xe6\xa0\xb8(?:\xe5\xb7\xb2\xe5\xae\x8c\xe6\x88\x90|\xe8\xaf\xb7\xe8\xa1\xa5\xe5\x85\x85\xe5\xbf\x85\xe8\xa6\x81\xe4\xbf\xa1\xe6\x81\xaf|\xe6\x9c\xac\xe6\xac\xa1\xe6\x9c\xaa\xe5\xae\x8c\xe6\x88\x90)\xe3\x80\x82\\n\\n<!-- maimi-project-v1:[a-f0-9-]{36} -->/i.test(part.text));\n      return {jobIds,salesOrder:salesOrder||approval,expectedConnector:approval?'sales-order-approval':salesOrder?'sales-orders':undefined,seq:match.event.seq};},\n"), (1779984, 1780080, b"  const keys=new Set(['meeting','infrastructure-monitor','heatcost','service','sales-orders','sales-order-approval']);\n"), (1819022, 1819155, b"    const availableActions=allActions.filter(a=>{\n      if(card.connector==='sales-order-approval'){\n        if(a?.kind!=='invoke'||a.connector!==card.connector)return false;\n        if(a.action==='approve')return card.approval_stage==='detail'&&opaque(a.arguments?.review_id)&&Object.keys(a.arguments).length===1;\n        return ['pending','detail'].includes(a.action);\n      }\n      return !(editorIds.has(a?.arguments?.order_id)&&['prepare','submit'].includes(a?.action));\n    });\n"), (1822667, 1822978, b"      !(meeting&&complete)&&extras.length?h('details',{'data-megmeet-result-details':'',open:card.connector==='sales-order-approval'?true:undefined,style:{marginTop:10}},h('summary',{style:{cursor:'pointer',fontSize:12}},card.status==='needs_input'?'\xe6\x9f\xa5\xe7\x9c\x8b\xe9\x9c\x80\xe8\xa6\x81\xe6\xa0\xb8\xe5\xaf\xb9\xe7\x9a\x84\xe4\xbf\xa1\xe6\x81\xaf':'\xe6\x9f\xa5\xe7\x9c\x8b\xe7\xbb\x93\xe6\x9e\x9c\xe8\xaf\xa6\xe6\x83\x85'),...extras.map((section,index)=>h(Section,{section,key:index}))):null,\n"), (1826281, 1826379, b"  const keys = new Set(['meeting','infrastructure-monitor','heatcost','service','sales-orders','sales-order-approval']);\n")]


def sales_review_client(company):
    if len(company)==1816843:
        for index,offset in reversed(((0, 63), (1, 119), (2, 119), (3, 119), (4, 119), (5, 119), (6, 119))):
            begin,end,data=SALES_EDITOR_PATCH[index]
            company=company[:begin+offset]+data+company[end+offset:]
        for index,offset in reversed(((0, 0), (1, 0), (2, 0), (4, 119), (5, 119), (6, 119), (7, 119))):
            begin,end,data=SALES_APPROVAL_PATCH[index]
            company=company[:begin+offset]+data+company[end+offset:]
        company=company.replace(b'      return {jobIds:jobs.map(job=>job.id),salesOrder:salesOrderIds.length>0,salesOrderIds,seq:match.event.seq};},',b"      const approval=jobs.length>0&&match.event.data?.message?.content?.some(part=>part?.type==='text'&&typeof part.text==='string'&&\n        /(?:^|\\n\\n)\xe9\x94\x80\xe5\x94\xae\xe8\xae\xa2\xe5\x8d\x95\xe5\xae\xa1\xe6\xa0\xb8(?:\xe5\xb7\xb2\xe5\xae\x8c\xe6\x88\x90|\xe8\xaf\xb7\xe8\xa1\xa5\xe5\x85\x85\xe5\xbf\x85\xe8\xa6\x81\xe4\xbf\xa1\xe6\x81\xaf|\xe6\x9c\xac\xe6\xac\xa1\xe6\x9c\xaa\xe5\xae\x8c\xe6\x88\x90)\xe3\x80\x82\\n\\n<!-- maimi-project-v1:[a-f0-9-]{36} -->/i.test(part.text));\n      return {jobIds:jobs.map(job=>job.id),salesOrder:salesOrderIds.length>0||approval,salesOrderIds,expectedConnector:approval?'sales-order-approval':undefined,seq:match.event.seq};},",1)
        return company
    if len(company) not in (1815823,1816724):
        return company
    # repair_company_bundle has already authenticated the exact size and digest.
    if len(company)==1815823:
        for begin,end,data in reversed(SALES_TIMELINE_PATCH):
            company=company[:begin]+data+company[end:]
    for begin,end,data in reversed(SALES_EDITOR_PATCH):
        company=company[:begin]+data+company[end:]
    for begin,end,data in reversed(SALES_APPROVAL_PATCH):
        company=company[:begin]+data+company[end:]
    return company


def stable_web_account_menu(company):
    # Prior worker bundles exposed transport expiry as an IAM sign-in action
    # in the sidebar. This runs after the signed company plugin is identified.
    label = "const label = state.status === 'expired' ? '重新登录' : state.status === 'released' ? '重新进入云端' : '个人云端';".encode()
    if label not in company:
        return company
    action = b"state.status !== 'ready' ? h('button', { role: 'menuitem', className: 'mm-skill-row', onClick: () => globalThis.__MEGMEET_WEB_SESSION__?.resume() }, label) : null"
    if company.count(label) != 1 or company.count(action) != 1:
        raise IAMFailure(502, 'personal_cloud_connection_asset_changed')
    fixed_label = "const label = '个人云端';".encode()
    fixed_action = ("['idle_released','refresh_waiting','recovery_failed','transport_unavailable','queue_expired',"
                    "'queue_cancel_unconfirmed','queue_cancelled','released'].includes(state.status) ? "
                    "h('button', { role: 'menuitem', className: 'mm-skill-row', onClick: () => "
                    "globalThis.__MEGMEET_WEB_SESSION__?.resume() }, '重新连接工作区') : null").encode()
    return company.replace(label, fixed_label, 1).replace(action, fixed_action, 1)


def repair_company_bundle(body):
    if len(body) > MAX_BUNDLE_BYTES or body.count(COMPANY_PREFIX) != 1:
        raise IAMFailure(502, 'personal_cloud_connection_asset_changed')
    offset = body.index(COMPANY_PREFIX)
    matches = [(size, body[offset:offset + size]) for size, digest in COMPANY_PINS
               if len(body) >= offset + size
               and hashlib.sha256(body[offset:offset + size]).hexdigest() == digest]
    if len(matches) != 1 or matches[0][1].count(APPLY_ANCHOR) != 1:
        raise IAMFailure(502, 'personal_cloud_connection_asset_changed')
    size, company = matches[0]
    company = badge_result_client(skill_picker_client(stable_web_account_menu(order_confirm_client(sales_review_client(company)))))
    patched = company.replace(APPLY_ANCHOR, APPLY_ANCHOR + APPLY_INSERT, 1)
    result = body[:offset] + patched + body[offset + size:]
    # A line insertion invalidates the generated combo source-map offsets.
    return re.sub(br'(?m)^//# sourceMappingURL=[^\r\n]*\r?\n?\Z', b'', result)


def quiet_web_connection_indicator(body):
    start = body.find(SETTINGS_PREFIX)
    if start < 0 or body.count(SETTINGS_PREFIX) != 1:
        return body
    module = body[start:start + SETTINGS_BYTES]
    if hashlib.sha256(module).hexdigest() != SETTINGS_SHA256:
        return body  # Other native versions retain their original presentation.
    prior = (b'if(connectionState===`connecting`||holdConnecting)connectionIndicator=`connecting`;'
             b'else if(connectionState===`disconnected`)connectionIndicator=`disconnected`;'
             b'else if(showRecovery)connectionIndicator=`recovered`;')
    if module.count(prior) != 1:
        raise IAMFailure(502, 'personal_cloud_connection_asset_changed')
    replacement = (b'if(connectionState===`disconnected`||connectionState===`connecting`&&'
                   b'navigator.onLine===false)connectionIndicator=`disconnected`;')
    result = body[:start] + module.replace(prior, replacement, 1) + body[start + SETTINGS_BYTES:]
    return re.sub(br'(?m)^//# sourceMappingURL=[^\r\n]*\r?\n?\Z', b'', result)


async def read_repaired_connection_bundle(response, validate, plan=(COMPANY_FILE,)):
    mime = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if (response.status_code != 200 or mime not in ('text/javascript', 'application/javascript')
            or response.headers.get('content-encoding', 'identity').lower() != 'identity'):
        raise IAMFailure(502, 'personal_cloud_connection_asset_invalid')
    body = bytearray()
    checked = 0
    async for chunk in response.aiter_raw():
        body.extend(chunk)
        if len(body) > MAX_BUNDLE_BYTES:
            raise IAMFailure(502, 'personal_cloud_connection_asset_invalid')
        if time.monotonic() - checked >= 5:
            await validate()
            checked = time.monotonic()
    await validate()
    result = bytes(body)
    if COMPANY_FILE in plan:
        result = repair_company_bundle(result)
    if SETTINGS_FILE in plan:
        result = quiet_web_connection_indicator(result)
    return result
