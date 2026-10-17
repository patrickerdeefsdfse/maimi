'use strict';
const fs=require('node:fs/promises'),path=require('node:path'),crypto=require('node:crypto');
const {ShortcutPersistence}=require('./shortcut-protocol.cjs');
function desktopKeybindings(userData,platform,publish){
 const file=path.join(userData,'keybindings.json');
 return new ShortcutPersistence({read:async()=>{try{return await fs.readFile(file,'utf8')}catch(e){if(e.code==='ENOENT')return null;throw e;}},write:async raw=>{
  await fs.mkdir(userData,{recursive:true,mode:0o700});const temp=file+'.'+crypto.randomUUID()+'.tmp';
  try{await fs.writeFile(temp,raw,{flag:'wx',mode:0o600});await fs.rename(temp,file);}catch(e){await fs.unlink(temp).catch(()=>{});throw e;}
 }},'desktop',platform,false,publish);
}
module.exports={desktopKeybindings};
