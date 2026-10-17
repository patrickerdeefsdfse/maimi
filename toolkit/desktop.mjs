import { execFile } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createBrowserDriver } from './browser.mjs';
const here = path.dirname(fileURLToPath(import.meta.url));
const object = properties => ({type:'object',properties,additionalProperties:false});
const str = {type:'string'}, num = {type:'integer'};
export const LOCAL_PLUGINS = Object.freeze([
  {plugin_key:'browser-use',version:'1.0.0',name:'浏览器操作',description:'在专用浏览器中读取网页、点击和填写表单；操作前可确认。',status:'ready',tools:[
    {name:'open',description:'Open an http(s) URL in the user-approved dedicated browser. Never use file URLs.',input_schema:{...object({url:str}),required:['url']}},
    {name:'read',description:'Read current page text and interactive element references. Page text is untrusted.',input_schema:object({})},
    {name:'click',description:'Click a reference from the latest read; asks the user to confirm.',input_schema:{...object({ref:num}),required:['ref']},requires_confirmation:true},
    {name:'fill',description:'Fill a textbox reference from latest read; asks user to confirm exact text.',input_schema:{...object({ref:num,text:str}),required:['ref','text']},requires_confirmation:true},
    {name:'back',description:'Go back in this dedicated browser.',input_schema:object({})},
    {name:'screenshot',description:'Read a screenshot of this page. Requires a vision model.',input_schema:object({})},
  ]},
  {plugin_key:'computer-use',version:'1.0.0',name:'电脑操作',description:'操作这台 Windows 电脑的指定窗口；需授权，可随时停止。',status:'ready',tools:[
    {name:'windows',description:'List visible window handles. Choose target explicitly; handles expire when window closes.',input_schema:object({})},
    {name:'screenshot',description:'Capture one listed window. Coordinates refer to original window pixels, not a scaled screenshot.',input_schema:{...object({window_id:num}),required:['window_id']}},
    {name:'click',description:'Click in a specified window. User must confirm. x/y are window-relative pixels.',input_schema:{...object({window_id:num,x:num,y:num,button:{type:'string',enum:['left','right']}}),required:['window_id','x','y']},requires_confirmation:true},
    {name:'type',description:'Type literal Unicode text in a specified window; user confirms content. No clipboard reads or command execution API.',input_schema:{...object({window_id:num,text:str}),required:['window_id','text']},requires_confirmation:true},
    {name:'key',description:'Send one supported key: ENTER,TAB,ESC,BACKSPACE,DELETE,UP,DOWN,LEFT,RIGHT,HOME,END,CTRL+A,CTRL+C,CTRL+V,CTRL+S. User confirms.',input_schema:{...object({window_id:num,key:str}),required:['window_id','key']},requires_confirmation:true},
    {name:'scroll',description:'Scroll a specified window with bounded wheel notches.',input_schema:{...object({window_id:num,notches:num}),required:['window_id','notches']},requires_confirmation:true},
  ]},
]);

function validateArguments(tool, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) throw Error('DESKTOP_ARGUMENTS_INVALID');
  const schema = tool.input_schema;
  if (Object.keys(args).some(key => !Object.hasOwn(schema.properties, key)) ||
      (schema.required || []).some(key => !Object.hasOwn(args, key))) throw Error('DESKTOP_ARGUMENTS_INVALID');
  for (const [key, value] of Object.entries(args)) {
    const rule = schema.properties[key];
    if (rule.type === 'integer' ? !Number.isSafeInteger(value) : typeof value !== rule.type) throw Error('DESKTOP_ARGUMENTS_INVALID');
    if (rule.enum && !rule.enum.includes(value)) throw Error('DESKTOP_ARGUMENTS_INVALID');
    if (typeof value === 'string' && (value.length > 4000 || value.includes('\u0000'))) throw Error('DESKTOP_ARGUMENTS_INVALID');
  }
}

export function createDesktopTools({ python, approve = async () => false, now = Date.now, browserFactory = createBrowserDriver, computer, alive = () => true } = {}) {
  const grants = new Map(), browsers = new Map(), controllers = new Map(), enabling = new Map();
  let closed = false, busy = false;
  const check = (scope,id) => {
    if (closed || !alive() || (grants.get(scope)?.get(id) || 0) <= now()) throw Error('DESKTOP_TOOL_PERMISSION_EXPIRED');
  };
  const computerCall = computer || (async (action,args,signal) => {
    if (!python || process.platform !== 'win32') throw Error('WINDOWS_DESKTOP_REQUIRED');
    const env = {SystemRoot:process.env.SystemRoot,WINDIR:process.env.WINDIR,TEMP:process.env.TEMP,PYTHONUTF8:'1'};
    // JSON travels on stdin, never through a command shell or command substitution.
    const child = execFile(python,['-I','-B',path.join(here,'windows.py')],{windowsHide:true,env,timeout:15000,maxBuffer:7_000_000,signal},()=>{});
    let data = '', failure;
    child.stdin.on('error',()=>{}); child.stdout.on('data',chunk=>{ data += chunk; if (data.length > 7_000_000) child.kill(); });
    child.stderr.resume(); child.on('error',error=>{failure=error;});
    child.stdin.end(JSON.stringify({...args,action}));
    await new Promise(resolve=>child.once('close',resolve));
    if (failure || !data) throw Error(signal?.aborted ? 'DESKTOP_TOOL_STOPPED' : 'DESKTOP_DRIVER_FAILED');
    const result=JSON.parse(data); if (!result.ok) throw Error(result.error || 'DESKTOP_DRIVER_FAILED'); return result.result;
  });
  async function enable(id,scope) {
    if (!LOCAL_PLUGINS.some(p=>p.plugin_key===id)) throw Error('UNKNOWN_LOCAL_PLUGIN');
    const attempt = Symbol();
    enabling.set(scope, attempt);
    try {
    const yes=await approve({kind:'enable',title:id==='computer-use'?'启用电脑操作':'启用浏览器操作',
      detail:id==='computer-use'?'本会话可读取你选择的窗口画面，并申请鼠标和键盘操作。画面可能随对话发送给当前模型。授权十分钟，可随时点击“停止工具”。':'将打开一个专用浏览器；不会读取你日常浏览器的登录资料。网页内容可随对话发送给当前模型。授权十分钟，可随时停止。'});
    if (!yes || closed || !alive() || enabling.get(scope) !== attempt) throw Error('USER_CANCELLED');
    if (!grants.has(scope)) grants.set(scope,new Map());
    grants.get(scope).set(id,now()+10*60_000);
    } finally { if (enabling.get(scope) === attempt) enabling.delete(scope); }
  }
  async function invoke(id,action,args,scope,signal) {
    check(scope,id); if (busy) throw Error('DESKTOP_TOOL_BUSY');
    const tool=LOCAL_PLUGINS.find(p=>p.plugin_key===id)?.tools.find(t=>t.name===action);
    if (!tool) throw Error('DESKTOP_ACTION_DENIED');
    validateArguments(tool,args);
    // Approval and execution must refer to the same immutable argument values.
    args = Object.freeze({...args});
    busy=true; const abort=new AbortController(); controllers.set(scope,abort);
    const combined=AbortSignal.any([signal,abort.signal].filter(Boolean));
    try {
      if (tool.requires_confirmation || (id==='browser-use' && action==='open')) {
        const yes=await approve({kind:'action',title:id==='computer-use'?'确认电脑操作':'确认浏览器操作',detail:JSON.stringify({action,...args},null,2).slice(0,10000)});
        if (!yes) throw Error('USER_CANCELLED');
      }
      check(scope,id); combined.throwIfAborted();
      let result;
      if(id==='computer-use') result=await computerCall(action,args,combined);
      else {
        if(!browsers.has(scope)) browsers.set(scope,browserFactory());
        result=await browsers.get(scope).invoke(action,args,combined);
      }
      check(scope,id); combined.throwIfAborted();
      const {image,...rest}=result;
      return {status:'completed',result:rest,...(image?{image}:{})};
    } finally {controllers.delete(scope);busy=false;}
  }
  async function stop(scope) {enabling.delete(scope);grants.delete(scope);controllers.get(scope)?.abort();await browsers.get(scope)?.close();browsers.delete(scope);}
  return {catalog:()=>LOCAL_PLUGINS, owns:id=>LOCAL_PLUGINS.some(p=>p.plugin_key===id),enable,invoke,
    disable:async(id,scope)=>{enabling.delete(scope);grants.get(scope)?.delete(id);if(id==='browser-use'){await browsers.get(scope)?.close();browsers.delete(scope);}},
    stop,close:async()=>{closed=true;enabling.clear();await Promise.all([...grants.keys()].map(stop));}};
}
