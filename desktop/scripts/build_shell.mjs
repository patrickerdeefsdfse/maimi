import {createRequire,isBuiltin} from 'node:module';
import {pathToFileURL} from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
const root=path.join(import.meta.dirname,'../shell');
const buildRequire=createRequire(path.join(import.meta.dirname,'package.json'));
const runtimeRequire=createRequire(path.join(import.meta.dirname,'../runtime/package.json'));
const {build}=await import(pathToFileURL(buildRequire.resolve('tsdown')).href);
const {packagedImportsPlugin,importPackageName}=await import('./desktop-bundle-imports.mjs');
const packages=new Set(['electron',...Object.keys(JSON.parse(fs.readFileSync(root+'/package.json')).dependencies)]);
const resolveRuntime={name:'maimi-runtime-resolution',resolveId(id){
  if(id.startsWith('.')||id.startsWith('/')||/^[A-Za-z]:/.test(id)||isBuiltin(id)||packages.has(importPackageName(id)))return;
  try{return runtimeRequire.resolve(id);}catch{}
}};
await build({cwd:root,config:false,entry:{main:'src/main.ts'},outDir:'lib',format:'esm',platform:'node',target:'es2024',fixedExtension:false,dts:false,clean:false,
 define:{'process.env.DSH_CLIENT_VERSION':JSON.stringify('0.2.2-rc.1')},
 deps:{neverBundle:[...packages]},plugins:[resolveRuntime,packagedImportsPlugin({packages,nodeBuiltins:true})]});
for(const name of ['preload-app','preload-welcome','preload-platform-account','preload-mandatory','preload-update-dialog']){
 await build({cwd:root,config:false,entry:{[name]:`src/${name}.ts`},outDir:'lib',format:'cjs',platform:'node',target:'es2024',fixedExtension:false,dts:false,clean:false,codeSplitting:false,
 deps:{neverBundle:['electron']},plugins:[packagedImportsPlugin({packages:new Set(['electron','events','timers','url']),nodeBuiltins:false})]});
}
console.log('DESKTOP_RC1_SHELL_REBUILT');
