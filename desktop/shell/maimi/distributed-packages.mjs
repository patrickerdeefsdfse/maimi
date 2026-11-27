import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import path from 'node:path';

export const MAX_ZIP=4*1024*1024;
const MAX_FILES=8*1024*1024;
const keyPattern=/^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const versionPattern=/^(?:0|[1-9]\d{0,3})(?:\.(?:0|[1-9]\d{0,3})){2}$/;
export const digest=data=>createHash('sha256').update(data).digest('hex');
function check(ok){if(!ok)throw Error('COMPANY_PLUGIN_PACKAGE_INVALID');}
function label(value,max){return typeof value==='string'&&value.length>0&&value.length<=max&&value.trim()===value&&!/[\u0000-\u001f\u007f]/.test(value);}
export function releaseRecord(row){
  check(row && typeof row.plugin_key==='string' && row.plugin_key.length<=64 && keyPattern.test(row.plugin_key));
  check(typeof row.version==='string'&&versionPattern.test(row.version)&&['native','skill'].includes(row.kind));
  check(label(row.name,100)&&label(row.description,500)&&typeof row.sha256==='string'&&/^[a-f0-9]{64}$/.test(row.sha256));
  const {plugin_key,version,kind,name,description,sha256}=row;
  const package_url='/v1/plugins/distribution/'+plugin_key+'/'+version+'/package?client=desktop';
  check(row.package_url===package_url);
  return {plugin_key,version,kind,name,description,sha256,package_url};
}

export async function decodePackage(row,zip,runtimeRoot){
  check(Buffer.isBuffer(zip)&&zip.length>0&&zip.length<=MAX_ZIP&&digest(zip)===row.sha256);
  const yauzl=createRequire(path.join(runtimeRoot,'package.json'))('yauzl');
  const reader=await new Promise((resolve,reject)=>yauzl.fromBuffer(zip,{lazyEntries:true,autoClose:false,strictFileNames:true},(err,value)=>err?reject(err):resolve(value)));
  const names=new Set(row.kind==='skill'?['manifest.json','SKILL.md']:['manifest.json','package.json','index.mjs']);
  try{
    check(reader.entryCount===names.size);
    const files=await new Promise((resolve,reject)=>{
      const result=new Map();let total=0;
      reader.once('error',reject);reader.once('end',()=>resolve(result));
      reader.on('entry',entry=>{
        (async()=>{
          const type=(entry.externalFileAttributes>>>16)&0xf000;
          check(names.has(entry.fileName)&&!result.has(entry.fileName)&&(entry.generalPurposeBitFlag&1)===0&&(!type||type===0x8000));
          total+=entry.uncompressedSize;check(total<=MAX_FILES);
          check(entry.fileName!=='SKILL.md'||entry.uncompressedSize<=128*1024);
          const stream=await new Promise((ok,no)=>reader.openReadStream(entry,(err,val)=>err?no(err):ok(val)));
          const chunks=[];let length=0;
          for await(const chunk of stream){length+=chunk.length;check(length<=entry.uncompressedSize);chunks.push(chunk);}
          check(length===entry.uncompressedSize);
          result.set(entry.fileName,new TextDecoder('utf-8',{fatal:true}).decode(Buffer.concat(chunks,length)));
          reader.readEntry();
        })().catch(reject);
      });
      reader.readEntry();
    });
    const m=JSON.parse(files.get('manifest.json'));
    check(['plugin_key','version','kind','name','description'].every(k=>m[k]===row[k]));
    if(row.kind==='native'){
      const pkg=JSON.parse(files.get('package.json'));
      check(pkg.type==='module'&&['index.mjs','./index.mjs'].includes(pkg.main)&&pkg.version===row.version);
      for(const key of ['scripts','dependencies','optionalDependencies','peerDependencies','devDependencies','bundledDependencies','bundleDependencies']){
        check(pkg[key]===undefined || (pkg[key]!==null && typeof pkg[key]==='object' && Object.keys(pkg[key]).length===0));
      }
      const code=files.get('index.mjs');check(code.trim());
      return {row,code};
    }
    const content=files.get('SKILL.md');check(content.trim());
    return {row,content};
  }finally{reader.close();}
}
