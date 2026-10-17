import {readFile,readdir,access} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {constants} from 'node:fs';
import {createRequire} from 'node:module';
process.env.NARB_DISABLE_NATIVE_CACHE='1';
const require=createRequire('/opt/dsh/package.json');
const p=JSON.parse(await readFile('/opt/personal-cloud/runtime020-source-pins.json'));
if(p.runtime!=='0.2.0-rc.2')throw Error('RUNTIME_PIN_INVALID');
if(process.version!=='v22.20.0')throw Error('UNREVIEWED_NODE_RUNTIME');
for(const [name,version] of Object.entries(p.packages)){
 const pkg=JSON.parse(await readFile('/opt/dsh/node_modules/'+name+'/package.json'));
 if(pkg.name!==name||pkg.version!==version)throw Error('PACKAGE_VERSION_MISMATCH');
 if((name==='@deepseek-ai/dsh'||name.startsWith('@deepseek-ai/dsh-'))&&version!==p.runtime)throw Error('MIXED_DSH_RUNTIME');
}
if(p.packages['@deepseek-ai/dsh']!=='0.2.0-rc.2')throw Error('DSH_RUNTIME_INVALID');
const observed=(await readdir('/opt/dsh/node_modules/@deepseek-ai')).filter(n=>!n.startsWith('.'));
if(observed.length!==Object.keys(p.packages).length)throw Error('PACKAGE_INVENTORY_CHANGED');
for(const [file,sha] of Object.entries(p.source_targets))if(createHash('sha256').update(await readFile(file)).digest('hex')!==sha)throw Error('SOURCE_PIN_MISMATCH:'+file);
await access('/opt/dsh/node_modules/@deepseek-ai/node-addon-system-linux-x64/bin/landlock-run',constants.X_OK);
require('node-addon-require-builtin');
const sharp=require('sharp');
await sharp({create:{width:2,height:2,channels:3,background:'white'}}).png().toBuffer();
try{await access('/opt/dsh/node_modules/@deepseek-ai/libreoffice-kit-wasm');throw Error('LOCAL_OFFICE_RUNTIME_NOT_EXPECTED');}catch(e){if(e.code!=='ENOENT')throw e;}
console.log(JSON.stringify({status:'VERIFIED',runtime:p.runtime,node:process.version,deepseek_packages:observed.length,source_targets:Object.keys(p.source_targets).length,sharp:true}));
