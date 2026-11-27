import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';

/** Verify component pins before packaging a prepared Windows application. */
export async function verifyWindowsSecurity(packed,pins) {
  const security=pins.windowsSecurityComponents;
  if (!security) return;
  const json=name=>JSON.parse(fs.readFileSync(path.join(packed,name),'utf8'));
  const sha=async name=>{
    const hash=createHash('sha256');
    for await(const block of fs.createReadStream(path.join(packed,name))) hash.update(block);
    return hash.digest('hex');
  };
  assert.equal(fs.readFileSync(path.join(packed,'version'),'utf8').trim(),pins.electron,'Windows Electron');
  const runtime='resources/runtime/primary-runtime';
  const primary=json(runtime+'/runtime.json');
  assert.equal(primary.node,security.nodeWindowsX64.version,'offline work-space Node');
  assert.equal(await sha(runtime+'/dependencies/node/bin/node.exe'),security.nodeWindowsX64.exeSha256,'bundled Node executable');
  assert.equal(primary.pythonPackages.Pillow,security.pillowWindowsCP312.version,'offline Pillow');
  assert.equal(primary.payloadDigest,security.primaryRuntimeDigest,'office tools cache identity');
  const modules='resources/app/dsh/node_modules/';
  for(const [name,entry] of Object.entries(security.npmOverrides)){
    assert.equal(json(modules+name+'/package.json').version,entry.version,name);
  }
  for(const [relative,version] of Object.entries(security.undiciByInstallPath)){
    assert.equal(json(relative+'/package.json').version,version,relative);
  }
  for(const [relative,expected] of Object.entries(security.bundledBrowserAssets)){
    assert.equal(await sha(relative),expected,relative);
  }
}
