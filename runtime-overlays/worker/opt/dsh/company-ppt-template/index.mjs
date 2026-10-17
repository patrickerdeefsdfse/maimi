// Native DSH bundled skill. The host's existing skill catalog and loader own
// semantic discovery; this module never scans chat text or selects a model.
import {createHash} from 'node:crypto';
import {createReadStream,lstatSync,readFileSync} from 'node:fs';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

export const name='megmeet-company-ppt-template';
export const inject=['skills'];
export const REVIEWED_RUNTIME='0.2.0-rc.2';
export const PROVIDER='megmeet-company-ppt-template';
export const SKILL_NAME='company-ppt-template';
export const TEMPLATE=Object.freeze({relativePath:'assets/company-template-v5.3.pptx',sizeBytes:22275510,sha256:'0e742339972cbfb37f9e5c10c8f907f0f6fd95cfcf5409118a076c7258869e99'});
export const DESCRIPTION='用户要求使用公司PPT模板、麦米模板、麦米电气模板、麦格米特/MEGMEET品牌模板制作或改写演示文稿时，读取实际公司PPT母版并按其版式生成可编辑PPTX。支持同义表达，无需手动选技能。用户明确不用公司模板、指定其他模板或要求保留当前上传文件版式时，优先遵循该选择；仅提到公司而不制作PPT时不适用。';
const ROOT=path.dirname(fileURLToPath(import.meta.url));
const MAX_SKILL_BYTES=32768;
const MAX_GUIDE_BYTES=128*1024;
const MAX_HELPER_BYTES=64*1024;
const BUNDLED_RANK=600;

function fail(code){throw Object.assign(new Error(code),{code});}
function directory(filename){const info=lstatSync(filename);if(!info.isDirectory()||info.isSymbolicLink())fail('COMPANY_PPT_DIRECTORY_INVALID');return info;}
function file(filename,maximum,exactSize){const info=lstatSync(filename);if(!info.isFile()||info.isSymbolicLink()||info.size<1||info.size>maximum||(exactSize!==undefined&&info.size!==exactSize))fail('COMPANY_PPT_RESOURCE_INVALID');return info;}
function identity(info){return [info.dev,info.ino,info.size,info.mtimeMs,info.ctimeMs].join(':');}
function resources(){
  directory(ROOT);directory(path.join(ROOT,'skills'));
  const base=path.join(ROOT,'skills',SKILL_NAME);directory(base);
  directory(path.join(base,'assets'));directory(path.join(base,'references'));directory(path.join(base,'scripts'));
  const skill=path.join(base,'SKILL.md'),guide=path.join(base,'references','template-guide.md'),asset=path.join(base,TEMPLATE.relativePath),helper=path.join(base,'scripts','company_ppt.py'),preview=path.join(base,'scripts','preview_pdf.py');
  file(skill,MAX_SKILL_BYTES);file(guide,MAX_GUIDE_BYTES);file(asset,TEMPLATE.sizeBytes,TEMPLATE.sizeBytes);file(helper,MAX_HELPER_BYTES);file(preview,MAX_HELPER_BYTES);
  return {base,skill,guide,asset,helper,preview};
}
export function skillBody(raw){
  if(typeof raw!=='string'||Buffer.byteLength(raw)>MAX_SKILL_BYTES||raw.includes('\0'))fail('COMPANY_PPT_SKILL_INVALID');
  const normalized=raw.replace(/^\uFEFF/,'');
  const match=/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/u.exec(normalized);
  if(!match)fail('COMPANY_PPT_SKILL_FRONTMATTER_REQUIRED');
  const names=[...match[1].matchAll(/^name:[ \t]*([a-z0-9-]+)[ \t]*\r?$/gm)];
  if(names.length!==1||names[0][1]!==SKILL_NAME)fail('COMPANY_PPT_SKILL_NAME_MISMATCH');
  const body=normalized.slice(match[0].length).trim();
  if(!body)fail('COMPANY_PPT_SKILL_EMPTY');
  return body;
}
async function verifyAsset(filename,signal){
  signal?.throwIfAborted();const before=file(filename,TEMPLATE.sizeBytes,TEMPLATE.sizeBytes);const hash=createHash('sha256');let bytes=0;
  const stream=createReadStream(filename,{highWaterMark:128*1024,signal});
  try{
    for await(const chunk of stream){signal?.throwIfAborted();bytes+=chunk.length;if(bytes>TEMPLATE.sizeBytes)fail('COMPANY_PPT_TEMPLATE_CHANGED');hash.update(chunk);}
  }finally{stream.destroy();}
  const after=file(filename,TEMPLATE.sizeBytes,TEMPLATE.sizeBytes);
  if(identity(before)!==identity(after)||bytes!==TEMPLATE.sizeBytes||hash.digest('hex')!==TEMPLATE.sha256)fail('COMPANY_PPT_TEMPLATE_CHANGED');
  return identity(after);
}
export function apply(ctx,config={}){
  if(!config||typeof config!=='object'||Array.isArray(config)||Object.keys(config).length)fail('COMPANY_PPT_CONFIGURATION_UNSUPPORTED');
  if(!ctx.skills||typeof ctx.skills.registerProvider!=='function')fail('COMPANY_PPT_SKILL_REGISTRY_REQUIRED');
  const installed=resources();
  const body=skillBody(readFileSync(installed.skill,'utf8'));
  const guide=readFileSync(installed.guide,'utf8');
  const helper=readFileSync(installed.helper,'utf8');
  const preview=readFileSync(installed.preview,'utf8');
  let verifiedIdentity;
  const definition={name:SKILL_NAME,description:DESCRIPTION,invocation:{modelInvocable:true,userInvocable:false},source:'bundled',provider:PROVIDER,path:installed.skill,resourceBase:{kind:'directory',path:installed.base}};
  ctx.skills.registerProvider(control=>({
    name:PROVIDER,
    async list(options={}){
      control.signal.throwIfAborted();options.signal?.throwIfAborted();
      return [{...definition,rank:BUNDLED_RANK,locator:SKILL_NAME,invocation:{...definition.invocation},resourceBase:{...definition.resourceBase}}];
    },
    async get(candidate,options={}){
      control.signal.throwIfAborted();options.signal?.throwIfAborted();
      const signal=options.signal?AbortSignal.any([control.signal,options.signal]):control.signal;
      if(candidate.name!==SKILL_NAME||candidate.locator!==SKILL_NAME)fail('COMPANY_PPT_SKILL_UNKNOWN');
      const current=resources();
      const raw=await readFile(current.skill,{encoding:'utf8',signal});
      if(skillBody(raw)!==body||await readFile(current.guide,{encoding:'utf8',signal})!==guide||await readFile(current.helper,{encoding:'utf8',signal})!==helper||await readFile(current.preview,{encoding:'utf8',signal})!==preview)fail('COMPANY_PPT_RELOAD_REQUIRED');
      // The 22MB template is hashed only when this skill is actually loaded,
      // not during every worker startup or ordinary-chat catalog refresh.
      if(identity(file(current.asset,TEMPLATE.sizeBytes,TEMPLATE.sizeBytes))!==verifiedIdentity)
        verifiedIdentity=await verifyAsset(current.asset,signal);
      control.signal.throwIfAborted();options.signal?.throwIfAborted();
      return {...definition,content:body,invocation:{...definition.invocation},resourceBase:{...definition.resourceBase}};
    },
  }));
}
