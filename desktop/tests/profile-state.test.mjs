import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {prepareDesktopProfile} from '../shell/maimi/profile-state.mjs';
const fixture=()=>{const root=fs.mkdtempSync(path.join(os.tmpdir(),'maimi-profile-r104-'));return {root,profileRoot:path.join(root,'desktop')};};
test('relogin and software upgrade preserve custom plugins, settings and files',()=>{
  const f=fixture();
  try {
    const first=prepareDesktopProfile({...f,bundles:['base','web'],patch:[{id:'model',config:{port:1}}]});
    const manifest=path.join(f.profileRoot,'package.json');
    const pkg=JSON.parse(fs.readFileSync(manifest));
    pkg.dependencies['my-plugin']='1.2.3';pkg.dsh.profile.bundles.push('my-plugin');pkg.custom={keep:true};
    fs.writeFileSync(manifest,JSON.stringify(pkg));
    const original=fs.readFileSync(manifest);
    fs.writeFileSync(path.join(f.profileRoot,'cordis.patch.yml'),'# own settings\n[]\n');
    fs.writeFileSync(path.join(f.profileRoot,'node_modules','custom'),'user plugin');
    prepareDesktopProfile({...f,bundles:['base','web'],patch:[{id:'model',config:{port:2}}]});
    assert.deepEqual(fs.readFileSync(manifest),original);
    assert.equal(JSON.parse(fs.readFileSync(first.overlay))[0].config.port,2);
    prepareDesktopProfile({...f,bundles:['base','web','im'],patch:[]});
    const next=JSON.parse(fs.readFileSync(manifest));
    assert.deepEqual(next.dependencies,pkg.dependencies);assert.deepEqual(next.custom,pkg.custom);
    assert.deepEqual(next.dsh.profile.bundles,['base','web','@megmeet/desktop-defaults','my-plugin','im']);
    assert.equal(fs.readFileSync(path.join(f.profileRoot,'cordis.patch.yml'),'utf8'),'# own settings\n[]\n');
    assert.equal(fs.readFileSync(path.join(f.profileRoot,'node_modules','custom'),'utf8'),'user plugin');
  } finally {fs.rmSync(f.root,{recursive:true,force:true});}
});
test('unowned existing profile is refused without overwriting user data',()=>{
  const f=fixture();try{fs.mkdirSync(f.profileRoot);fs.writeFileSync(path.join(f.profileRoot,'package.json'),'original');
    assert.throws(()=>prepareDesktopProfile({...f,bundles:['base'],patch:[]}),/OWNER_MISSING/);
    assert.equal(fs.readFileSync(path.join(f.profileRoot,'package.json'),'utf8'),'original');
  }finally{fs.rmSync(f.root,{recursive:true,force:true});}
});
test('partial first creation resumes; linked managed directory is refused',()=>{
  const f=fixture();try{
    fs.mkdirSync(f.profileRoot);fs.writeFileSync(path.join(f.profileRoot,'.maimi-profile'),'maimi-persistent-desktop-profile/v1');
    const outside=path.join(f.root,'outside');fs.mkdirSync(outside);
    fs.symlinkSync(outside,path.join(f.profileRoot,'.maimi-managed'),'junction');
    assert.throws(()=>prepareDesktopProfile({...f,bundles:['base'],patch:[]}),/DIRECTORY_INVALID/);
    assert.deepEqual(fs.readdirSync(outside),[]);
  }finally{fs.rmSync(f.root,{recursive:true,force:true});}
});
