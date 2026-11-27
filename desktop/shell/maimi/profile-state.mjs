import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const MARKER='maimi-persistent-desktop-profile/v1';
const DEFAULTS='@megmeet/desktop-defaults';
const DEFAULTS_DEPENDENCY='link:.maimi-managed/defaults';
function directory(file) {
  if (!fs.existsSync(file)) fs.mkdirSync(file);
  const st=fs.lstatSync(file);
  if (!st.isDirectory() || st.isSymbolicLink()) throw Error('PROFILE_DIRECTORY_INVALID');
}
function ordinary(file) {
  const st=fs.lstatSync(file);
  if (!st.isFile() || st.isSymbolicLink()) throw Error('PROFILE_FILE_INVALID');
  if (st.size>1024*1024) throw Error('PROFILE_FILE_TOO_LARGE');
  return fs.readFileSync(file,'utf8');
}
function writeOwned(file,text) {
  if (fs.existsSync(file)) ordinary(file);
  const pending=file+'.'+crypto.randomUUID()+'.tmp';
  fs.writeFileSync(pending,text,{flag:'wx',mode:0o600});
  try { fs.renameSync(pending,file); }
  catch(error) { fs.unlinkSync(pending); throw error; }
}
/** Company defaults are a bundle below the user's patch, so setting changes
 * survive restarts. Only this named, marked bundle is regenerated. */
export function prepareDesktopProfile({profileRoot,bundles,patch}) {
  if (!path.isAbsolute(profileRoot)||!Array.isArray(bundles)||!Array.isArray(patch)||
      !bundles.every(x=>typeof x==='string'&&x.length>0)) throw Error('PROFILE_ARGUMENTS');
  directory(profileRoot);
  const marker=path.join(profileRoot,'.maimi-profile');
  const manifest=path.join(profileRoot,'package.json');
  if (fs.existsSync(marker)) {
    if (ordinary(marker)!==MARKER) throw Error('PROFILE_OWNER_MISMATCH');
  } else {
    // Never adopt an unrelated existing profile. A partial first creation can
    // resume after its marker was durably written.
    if (fs.readdirSync(profileRoot).length) throw Error('PROFILE_OWNER_MISSING');
    fs.writeFileSync(marker,MARKER,{flag:'wx',mode:0o600});
  }
  const managed=path.join(profileRoot,'.maimi-managed');
  directory(managed);
  const source=path.join(managed,'defaults');directory(source);
  const modules=path.join(profileRoot,'node_modules');directory(modules);
  const scope=path.join(modules,'@megmeet');directory(scope);
  const installed=path.join(scope,'desktop-defaults');
  if (!fs.existsSync(installed)) fs.symlinkSync(source,installed,process.platform==='win32'?'junction':'dir');
  const linked=fs.lstatSync(installed).isSymbolicLink();
  if (linked && fs.realpathSync(installed)!==fs.realpathSync(source)) throw Error('DEFAULTS_LINK_TARGET_INVALID');
  if (!linked) directory(installed);
  for (const target of linked?[source]:[source,installed]) {
    const owner=path.join(target,'.maimi-defaults');
    if (fs.existsSync(owner)) {
      if(ordinary(owner)!==MARKER) throw Error('DEFAULTS_OWNER_MISMATCH');
    } else {
      if(fs.readdirSync(target).length) throw Error('DEFAULTS_OWNER_MISSING');
      fs.writeFileSync(owner,MARKER,{flag:'wx',mode:0o600});
    }
    writeOwned(path.join(target,'package.json'),JSON.stringify({name:DEFAULTS,version:'1.0.0',private:true,dsh:{bundle:{patch:'company.patch.yml'}}},null,2)+'\n');
    writeOwned(path.join(target,'company.patch.yml'),JSON.stringify(patch,null,2)+'\n');
  }
  const required=[...bundles,DEFAULTS];
  if (!fs.existsSync(manifest)) {
    writeOwned(manifest,JSON.stringify({name:'maimi-desktop-user-profile',private:true,
      dependencies:{[DEFAULTS]:DEFAULTS_DEPENDENCY},
      dsh:{profile:{bundles:required}}},null,2)+'\n');
  } else {
    const pkg=JSON.parse(ordinary(manifest));
    const current=pkg?.dsh?.profile?.bundles;
    if (!Array.isArray(current)||!current.every(x=>typeof x==='string')) throw Error('PROFILE_MANIFEST_INVALID');
    const missing=required.filter(x=>!current.includes(x));
    const dependency=pkg.dependencies?.[DEFAULTS];
    if(dependency!==undefined&&dependency!==DEFAULTS_DEPENDENCY&&dependency!=='file:.maimi-managed/defaults') throw Error('DEFAULTS_DEPENDENCY_CONFLICT');
    if (missing.length || dependency!==DEFAULTS_DEPENDENCY) {
      pkg.dsh.profile.bundles=[...current,...missing];
      pkg.dependencies={...pkg.dependencies,[DEFAULTS]:DEFAULTS_DEPENDENCY};
      writeOwned(manifest,JSON.stringify(pkg,null,2)+'\n');
    }
  }
  return {bundle:DEFAULTS,overlay:path.join(installed,'company.patch.yml')};
}
