// Test-only path resolution. No company endpoints or user-state discovery.
import path from 'node:path';
import fs from 'node:fs';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
export function resolveRuntime(desktop=false){
 const override=desktop?(process.env.DSH_R65_DESKTOP_RUNTIME??process.env.DSH_R65_RUNTIME):(process.env.DSH_R65_RUNTIME??process.env.DSH_R65_DESKTOP_RUNTIME);
 const runtime=path.resolve(override??path.join(import.meta.dirname,'../../../upstream',process.platform==='win32'?'runtime-win':'runtime-linux'));
 const pkg=JSON.parse(fs.readFileSync(path.join(runtime,'node_modules/@deepseek-ai/dsh/package.json'),'utf8'));
 if(pkg.version!=='0.2.1-rc.2')throw Error('PINNED_DSH_0_2_1_RC2_REQUIRED');
 return runtime;
}
export function loadChromium(){
 try{return require(process.env.MAIMI_PLAYWRIGHT_MODULE??'playwright').chromium;}
 catch{throw Error('PLAYWRIGHT_REQUIRED: install the documented test dependency or set MAIMI_PLAYWRIGHT_MODULE');}
}
export function chromiumOptions(){return {headless:true,...(process.env.MAIMI_CHROMIUM_EXE?{executablePath:process.env.MAIMI_CHROMIUM_EXE}:{})};}
