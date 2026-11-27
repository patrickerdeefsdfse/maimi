import {defineReadDocumentTool} from './upstream.mjs';
import {surfaceOf} from '../policy.mjs';
import path from 'node:path';
export const name='maimi-documents';
export const inject=['tools','systemPrompt'];
export const DOCUMENT_LIMITS=Object.freeze({maxFileBytes:24*1024*1024,readLimit:400,sheetRowLimit:200,maxSheets:5,maxOutputChars:16000,readTimeoutMs:60000});
export function requiresWps(args){return /\.(?:docm?|docx|xlsm?|xlsx|xlsb|pptm?|pptx|wps|wpt|et|ett|dps|dpt)$/i.test(args.file_path??'')||(/\.pdf$/i.test(args.file_path??'')&&args.unencrypted!==true);}
/** Only the dedicated execution process may use the original parser. No library scan/copy/delete routes. */
export function executionDocumentTool(ctx){return defineReadDocumentTool(ctx,DOCUMENT_LIMITS);}
export function managedDocumentTool(ctx,config={}){
 const surface=surfaceOf(config),original=executionDocumentTool(ctx);
 return {...original,parameters:{...original.parameters,unencrypted:{type:'boolean',description:'Desktop PDF only: true only when the user explicitly confirms this is an ordinary unencrypted PDF.'}},async execute(args,exec){
  if(surface==='web'){
   const candidate=args.file_path;
   if(typeof candidate!=='string'||candidate.includes('\0')||candidate.includes('\\')||!candidate.startsWith('/workspace/')||!path.posix.normalize(candidate).startsWith('/workspace/'))throw Error('DOCUMENT_WORKSPACE_PATH_REQUIRED');
   const sessionId=exec.agent?.session?.id??exec.agent?.session?.header?.id;
   if(typeof sessionId!=='string'||!sessionId)throw Error('DOCUMENT_SESSION_REQUIRED');
   const service=ctx.get?.('maimiExecution');
   const run=config.readDocument??service?.readDocument?.bind(service);
   if(typeof run!=='function')throw Error('DOCUMENT_EXECUTION_NOT_CONFIGURED');
   return run(args,{sessionId,signal:exec.signal});
  }
  if(requiresWps(args))throw Error('WPS_READ_REQUIRED: 请先调用 wps-read 技能，使用获公司授权的本机 WPS 读取原始文件。普通解析器不会代读企业加密文件。');
  return original.execute(args,exec);
 }};
}
export function apply(ctx,config={}){
 surfaceOf(config);ctx.effect(()=>ctx.tools.register(managedDocumentTool(ctx,config)),'maimi: bounded document reading');
 ctx.systemPrompt.section({name:'maimi:documents',order:110,text:config.surface==='web'?'Use read_document for PDF/Word/Excel files. Parsing runs only in the on-demand execution environment and uses this session’s authorized files. Request a small page first; do not create permanent extracted copies.':'For office documents and company PDFs invoke wps-read first. read_document is for plain text and explicitly confirmed unencrypted PDFs. Never use another parser to bypass corporate encryption.'});
}
