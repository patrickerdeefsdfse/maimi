import fs from 'node:fs';
import path from 'node:path';
import {writeDesktopRuntimeAsync, verifyDesktopRuntime} from './runtime-tree.mjs';
const root=process.argv[2];
const descriptor=JSON.parse(fs.readFileSync(path.join(root,'desktop-runtime.json')));
if(descriptor.release.version!=='0.2.2-rc.5') throw Error('Unexpected DSH release');
if (!process.argv.includes('--verify-only')) {
  await writeDesktopRuntimeAsync(root,descriptor.release,descriptor.sharedPackages.map(row=>row.name));
}
const result=await verifyDesktopRuntime(root,descriptor.release.version,{platform:'win32',arch:'x64'});
console.log(JSON.stringify({files:result.files.length,sharedPackages:result.sharedPackages.length,verified:true}));
