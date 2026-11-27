import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {surfaceOf} from './policy.mjs';
export function managedPluginProfile({surface,runtimeRoot='/opt/dsh',pluginRoot=path.join(runtimeRoot,'plugins/managed/runtime'),workspaceRoot='/workspace',authorizedExtraGeneration=false,browserConfig={}}={}) {
 surfaceOf({surface});
 const entry=(id,file,config)=>({id,name:pathToFileURL(path.join(pluginRoot,file)).href,config});
 return [{insert:[entry('maimi-managed-plugins','index.mjs',{surface}),entry('maimi-context','context/index.mjs',{surface,workspaceRoot}),entry('maimi-documents','files/index.mjs',{surface}),entry('maimi-genui','genui/index.mjs',{surface}),...(surface==='desktop'?[entry('dsh-market','market/lib/index.js',{profile:'desktop'}),entry('maimi-browser','browser/index.mjs',{...browserConfig,surface}),entry('maimi-better-sidebar','sidebar/index.mjs',{surface,authorizedExtraGeneration})]:[])]}];
}
