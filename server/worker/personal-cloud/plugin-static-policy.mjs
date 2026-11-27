// Fixed GenUI 0.14.0 engine routes only. Called after the worker bearer check.
// No filesystem path, plugin-management API or install route is granted here.
export const GENUI_ASSET_PREFIX='/plugins/@changfenhuang/dsh-genui/assets/';
export const GENUI_ASSET_FILES=Object.freeze(['echarts-core.js','echarts-full.js','icons.js','mermaid.js','standalone-runtime.js','three.js']);
const paths=new Set(GENUI_ASSET_FILES.map(name=>GENUI_ASSET_PREFIX+name));
export function pluginStaticAllowed(method,rawUrl,headers={}){
 const question=rawUrl.indexOf('?'),path=question<0?rawUrl:rawUrl.slice(0,question),query=question<0?'':rawUrl.slice(question+1);
 let decoded;try{decoded=decodeURIComponent(path);}catch{return false;}
 const candidate=path.startsWith('/plugins/@changfenhuang/dsh-genui')||decoded.startsWith('/plugins/@changfenhuang/dsh-genui');
 if(!candidate)return true; // Other reviewed native routes keep their existing guards.
 return ['GET','HEAD'].includes(method)&&paths.has(path)&&decoded===path
  &&(query===''||/^rev=[a-f0-9]{12}$/.test(query))&&!headers.range;
}
