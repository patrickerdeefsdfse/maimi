import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const require=createRequire(path.join(import.meta.dirname,'package.json'));
const {build}=await import(pathToFileURL(require.resolve('tsdown')).href);
await build({cwd:path.join(import.meta.dirname,'../host-package'),config:false,
  entry:{index:'src/company-exports.ts',cli:'src/cli.ts'},outDir:'lib',format:'esm',platform:'node',target:'es2024',
  fixedExtension:false,dts:false,clean:false,codeSplitting:false});
console.log('RC1_PRIVATE_HOST_BUILT_FROM_PINNED_SOURCE');

