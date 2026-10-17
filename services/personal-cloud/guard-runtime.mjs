// Image-only fixed-workspace policy; runs below HTTP AND native mux transports.
import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const file='/opt/dsh/node_modules/@deepseek-ai/dsh-api-workspace-controller/lib/index.js';
const pkg=JSON.parse(await readFile('/opt/dsh/node_modules/@deepseek-ai/dsh-api-workspace-controller/package.json','utf8'));
if(pkg.version!=='0.1.6-alpha.2') throw new Error('cloud workspace adapter requires review for this DSH version');
let text=await readFile(file,'utf8');
if(createHash('sha256').update(text).digest('hex')!=='1da4fd5c718fb7f9ea6ffee9285060dc446803f6b2ff65e8f5935a5c2cefd641') throw new Error('cloud workspace upstream source changed; review required');
for(const [anchor,check] of [
 ['\tcreate(request) {\n\t\treturn this.enqueue',`\tcreate(request) {\n\t\tif (request.path !== '/workspace') return Promise.reject(new RemoteError('workspace/cloud-fixed', '云端工作区路径不可更改', {}));\n\t\treturn this.enqueue`],
 ['\trename(request) {\n\t\tconst title',`\trename(request) {\n\t\tif (request.title !== '云端工作区') return Promise.reject(new RemoteError('workspace/cloud-fixed', '云端工作区名称不可更改', {}));\n\t\tconst title`],
 ['\tdelete(request) {\n\t\treturn this.enqueue',`\tdelete(request) {\n\t\treturn Promise.reject(new RemoteError('workspace/cloud-fixed', '云端工作区不可删除', {}));\n\t\treturn this.enqueue`],
]) {
 if(text.split(anchor).length!==2) throw new Error('native workspace policy anchor changed');
 text=text.replace(anchor,check);
}
await writeFile(file,text);
console.log('PERSONAL_CLOUD_FIXED_WORKSPACE_GUARD: PASS');
