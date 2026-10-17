'use strict';
const fs=require('node:fs'), path=require('node:path'), crypto=require('node:crypto');
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
function verifyToolkit(runtimeRoot,policy=require('./toolkit-policy.json')){
  const root=path.join(runtimeRoot,'toolkit'), bytes=fs.readFileSync(path.join(root,'toolkit-manifest.json'));
  if(sha(bytes)!==policy.manifestSha256)throw Error('插件组件清单不匹配，请重新安装软件。');
  const m=JSON.parse(bytes);
  if(m.protocol!=='megmeet-connectors-v1'||m.version!==policy.version)throw Error('插件组件版本不兼容。');
  for(const [name,expected] of Object.entries(m.files)){
    if(!name||name.includes('\\')||name.startsWith('/')||name.split('/').some(x=>['','..','.'].includes(x)))throw Error('插件组件路径异常。');
    const file=path.join(root,...name.split('/'));
    if(fs.lstatSync(file).isSymbolicLink())throw Error('插件组件链接无效。');
    const data=fs.readFileSync(file);
    if(data.length!==expected.bytes||sha(data)!==expected.sha256)throw Error('插件组件校验失败：'+name);
  }
  return root;
}
module.exports={verifyToolkit};
