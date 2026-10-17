'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
// Stored inside the already authenticated account directory. This file grants
// no authority and never contains a token, workspace id or business content.
function workspacePreference(home) {
  const file=path.join(home,'workspace-preference.json');
  function read() {
    try {
      const stat=fs.lstatSync(file);
      if(!stat.isFile()||stat.isSymbolicLink()||stat.nlink!==1||stat.size>256)return 'local';
      const value=JSON.parse(fs.readFileSync(file,'utf8'));
      return value.schema===1&&value.mode==='cloud'?'cloud':'local';
    }catch{return 'local';}
  }
  function write(mode) {
    if(!['local','cloud'].includes(mode))throw Error('WORKSPACE_MODE_INVALID');
    const temporary=file+'.tmp-'+crypto.randomUUID();
    let owned=false;
    try {
      fs.writeFileSync(temporary,JSON.stringify({schema:1,mode}),{flag:'wx',mode:0o600});
      owned=true;
      fs.renameSync(temporary,file);
    }catch {
      // Failure to remember a preference cannot erase a working session.
    }finally {if(owned)try{fs.unlinkSync(temporary);}catch{}}
  }
  return {read,write};
}
module.exports={workspacePreference};
