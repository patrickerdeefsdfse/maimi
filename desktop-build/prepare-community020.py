from pathlib import Path
root=Path(__file__).resolve().parent
p=root/'app/community-profile.mjs'
s=p.read_text(encoding='utf-8')
anchor='  await mkdir(dir,{recursive:true});'
assert s.count(anchor)==1
call="\n  await prepareReviewedCompatibility({dir,runtimeRoot});"
helper=r'''

// The distribution pins and reviews this guarded community plugin against
// this exact native release. Use DSH's profile-local exact-version mechanism;
// never weaken the native compatibility gate for other plugins/releases.
async function prepareReviewedCompatibility({dir,runtimeRoot}) {
  const runtime=JSON.parse(await readFile(path.join(runtimeRoot,'node_modules/@deepseek-ai/dsh/package.json'),'utf8'));
  const plugin=JSON.parse(await readFile(path.join(runtimeRoot,'node_modules/@dshp/token-meter/package.json'),'utf8'));
  if(runtime.version!=='0.2.0-rc.2'||plugin.name!=='@dshp/token-meter'||plugin.version!=='0.8.0')throw Error('COMMUNITY_COMPATIBILITY_NOT_REVIEWED');
  const file=path.join(dir,'compatibility.json');let text,prior={};
  try {const info=await lstat(file);if(!info.isFile()||info.isSymbolicLink()||info.nlink!==1||info.size>1024*1024)throw Error('COMMUNITY_COMPATIBILITY_INVALID');text=await readFile(file,'utf8');prior=JSON.parse(text);}
  catch(error){if(error.code!=='ENOENT')throw Error('COMMUNITY_COMPATIBILITY_INVALID');}
  const version=/^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
  if(prior===null||typeof prior!=='object'||Array.isArray(prior))throw Error('COMMUNITY_COMPATIBILITY_INVALID');
  for(const [key,values] of Object.entries(prior)){
    const cut=key.lastIndexOf('@'),name=key.slice(0,cut);
    if(cut<=0||!/^(@[a-z0-9._-]+\/)?[a-z0-9._-]+$/.test(name)||!version.test(key.slice(cut+1))||!Array.isArray(values)||values.some(v=>typeof v!=='string'||!version.test(v)))throw Error('COMMUNITY_COMPATIBILITY_INVALID');
  }
  const key='@dshp/token-meter@0.8.0';
  if(prior[key]?.includes('0.2.0-rc.2'))return;
  const next={...prior,[key]:[...(prior[key]||[]),'0.2.0-rc.2']};
  if(text!==undefined){try{await copyFile(file,file+'.before-megmeet-dsh020',constants.COPYFILE_EXCL);}catch(error){if(error.code!=='EEXIST')throw error;}}
  const temporary=file+'.megmeet-'+randomUUID()+'.tmp';
  await writeFile(temporary,JSON.stringify(next,null,2)+'\n',{flag:'wx',mode:0o600});await rename(temporary,file);
}
'''
# This remains a candidate until the independent API audit and real host/UI
# test succeed; the runtime package bytes and compatibility gate stay intact.
s=s.replace(anchor,anchor+call)+helper
(root/'community-profile020.candidate.mjs').write_text(s,encoding='utf-8')
print('COMMUNITY_COMPATIBILITY_REVIEW_CANDIDATE')
