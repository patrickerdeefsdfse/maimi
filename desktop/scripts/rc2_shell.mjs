import fs from 'node:fs';
import path from 'node:path';
import {createRequire,isBuiltin} from 'node:module';
import {pathToFileURL} from 'node:url';
import {packagedImportsPlugin,importPackageName} from './desktop-bundle-imports.mjs';
const repo=path.resolve(import.meta.dirname,'..');
const work=path.resolve(process.env.MAIMI_WORK);
const releaseVersion=JSON.parse(fs.readFileSync(path.join(repo,'deps/pins.json'))).dsh.release;
const app=path.join(work,'windows/win-unpacked/resources/app');
const tool=createRequire(process.env.MAIMI_BUILD_TOOLS_PACKAGE??path.join(import.meta.dirname,'package.json'));
const staged=createRequire(path.join(app,'dsh/package.json'));
const ui=createRequire(path.join(app,'package.json'));
const {build}=await import(pathToFileURL(tool.resolve('tsdown')).href);
const external=new Set(['electron',...Object.keys(JSON.parse(fs.readFileSync(path.join(app,'package.json'))).dependencies)]);
const resolver={name:'company-materialized-runtime',resolveId(id){
  if(id.startsWith('.')||path.isAbsolute(id)||isBuiltin(id)||external.has(importPackageName(id)))return;
  if(id==='@deepseek-ai/dsh-util-file-drag')return path.join(repo,'shell/vendor/file-drag.ts');
  if(id==='mime')return path.join(work,'source-assets/mime/package/dist/src/index.js');
  try{return staged.resolve(id);}catch{}
  try{return ui.resolve(id);}catch{}
}};
const utilities=path.join(work,'tools');
fs.mkdirSync(utilities,{recursive:true});
const tsconfig=path.join(utilities,'tsconfig.json');
fs.writeFileSync(tsconfig,JSON.stringify({compilerOptions:{target:'ES2024',module:'ESNext',moduleResolution:'Bundler',allowImportingTsExtensions:true}}));
const common={cwd:path.join(repo,'shell'),config:false,tsconfig,platform:'node',target:'es2024',dts:false,clean:false,codeSplitting:false};
for (const [name,format,packages] of [['main','esm',external],['preload-app','cjs',new Set(['electron','events','timers','url'])]]){
  await build({...common,entry:{[name]:'src/'+name+'.ts'},outDir:path.join(app,'lib'),format,fixedExtension:false,
    define:{'process.env.DSH_CLIENT_VERSION':JSON.stringify(releaseVersion)},deps:{neverBundle:[...packages]},
    plugins:[resolver,packagedImportsPlugin({packages,nodeBuiltins:name==='main'})]});
}
await build({...common,entry:{'file-drag':path.join(repo,'host-package/src/file-drag.ts')},outDir:path.join(app,'maimi/upstream-native'),
  format:'esm',fixedExtension:true,plugins:[resolver]});
// Keep the sealing helper in its own build directory. Its external semver import resolves to the installed distribution.
await build({...common,entry:{'runtime-tree':path.join(repo,'shell/src/runtime-tree.ts')},outDir:utilities,format:'esm',fixedExtension:true,plugins:[resolver]});
const semver=path.join(utilities,'node_modules/semver');
if(!fs.existsSync(semver))fs.cpSync(path.join(app,'node_modules/semver'),semver,{recursive:true});
const {writeDesktopRuntimeAsync,verifyDesktopRuntime}=await import(pathToFileURL(path.join(utilities,'runtime-tree.mjs')));
const root=path.join(app,'dsh'),original=JSON.parse(fs.readFileSync(path.join(root,'desktop-runtime.json')));
if(original.release.version!==releaseVersion)throw Error('Unexpected upstream runtime');
await writeDesktopRuntimeAsync(root,original.release,original.sharedPackages.map(p=>p.name));
const result=await verifyDesktopRuntime(root,original.release.version,{platform:'win32',arch:'x64'});
console.log(JSON.stringify({version:result.release.version,files:result.files.length,sharedPackages:result.sharedPackages.length}));
