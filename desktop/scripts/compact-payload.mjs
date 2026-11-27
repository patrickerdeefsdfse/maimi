import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

const run = promisify(execFile);

// Keep the updater's installer blockmap. Small solid blocks compress related
// files together while bounding the amount a changed file can recompress.
export function useCompactPayload(builderRequire, packed, x64) {
  const {NsisTarget} = builderRequire('app-builder-lib/out/targets/nsis/NsisTarget.js');
  const {getPath7za} = builderRequire('app-builder-lib/out/toolsets/7zip.js');
  NsisTarget.prototype.buildAppPackage = async function (directory, arch) {
    if (path.resolve(directory) !== path.resolve(packed) || arch !== x64 || this.isWebInstaller)
      throw Error('Unexpected Windows payload target');
    if (!fs.existsSync(path.join(directory, 'resources/elevate.exe')))
      throw Error('Missing Windows installer handoff helper');
    const archive = path.join(this.outDir, 'maimi-payload-x64.nsis.7z');
    if (fs.existsSync(archive)) throw Error('Windows payload already exists');
    const zip = await getPath7za();
    await run(zip, ['a','-t7z','-mx=5','-md=8m','-ms=16m','-mmt=4','-mf=BCJ',
      '-mtc=off','-mta=off','-mtm=off','-bso0','-bsp0',archive,'.'],
      {cwd: directory, windowsHide: true});
    await run(zip, ['t','-bso0','-bsp0',archive], {windowsHide:true});
    const hash = createHash('sha512');
    for await (const block of fs.createReadStream(archive)) hash.update(block);
    return {path:archive, size:fs.statSync(archive).size, sha512:hash.digest('base64')};
  };
}
