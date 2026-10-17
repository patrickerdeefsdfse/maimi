import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const vendor = path.join(here, 'vendor-community');
const guardFile = path.join(here, 'community-plugins-guard.mjs');
export const COMMUNITY_COMMIT = 'b9ea3d7625a7ec40cf191094e7b83571ba5c0a79';
export const COMMUNITY_DEFAULTS = Object.freeze({
  'file-change-viewer': { patchTool: false },
  'web-style': { themeId: '', radius: { global: -1 } },
  'widget-kit': { referenceWidgets: false },
  'token-meter': { vendors: [], activeVendor: '' },
});
const sha = data => crypto.createHash('sha256').update(data).digest('hex');

export function verifyCommunitySource(directory = vendor) {
  const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
  if (manifest.schema !== 1 || manifest.commit !== COMMUNITY_COMMIT || manifest.dsh !== '0.1.6-alpha.2') throw Error('Community source version mismatch');
  if (!Array.isArray(manifest.plugins) || manifest.plugins.length !== 4) throw Error('Community plugin roster mismatch');
  const seen = new Set();
  for (const plugin of manifest.plugins) {
    if (!(plugin.directory in COMMUNITY_DEFAULTS) || seen.has(plugin.directory) || plugin.name !== '@dshp/' + plugin.directory) throw Error('Community plugin identity mismatch');
    seen.add(plugin.directory);
    for (const [relative, digest] of Object.entries(plugin.files)) {
      if (!/^(?:package\.json|README\.md|LICENSE|cordis\.patch\.yml|lib\/[A-Za-z0-9_.\/-]+)$/.test(relative) || relative.split('/').includes('..')) throw Error('Community path denied');
      const file = path.join(directory, plugin.directory, relative), stat = fs.lstatSync(file);
      if (!stat.isFile() || stat.isSymbolicLink() || sha(fs.readFileSync(file)) !== digest) throw Error('Community artifact integrity differs: ' + plugin.name + '/' + relative);
    }
    const pkg = JSON.parse(fs.readFileSync(path.join(directory, plugin.directory, 'package.json'), 'utf8'));
    if (pkg.name !== plugin.name || pkg.version !== plugin.version || pkg.license !== 'MIT') throw Error('Community package metadata differs');
  }
  return { ...manifest, manifestSha256: sha(fs.readFileSync(path.join(directory, 'manifest.json'))) };
}

export function stageCommunity(runtimeDirectory) {
  const runtime = path.resolve(runtimeDirectory), manifest = verifyCommunitySource();
  const guard = fs.readFileSync(guardFile), guardSha256 = sha(guard);
  const pkg = JSON.parse(fs.readFileSync(path.join(runtime, 'node_modules/@deepseek-ai/dsh/package.json'), 'utf8'));
  if (pkg.version !== manifest.dsh) throw Error('Community integration requires tested DSH version');
  for (const plugin of manifest.plugins) {
    const destination = path.join(runtime, 'node_modules', plugin.name);
    fs.mkdirSync(destination, { recursive: true });
    if (fs.lstatSync(destination).isSymbolicLink()) throw Error('Community destination cannot be a link');
    for (const [relative, expected] of Object.entries(plugin.files)) {
      const target = path.join(destination, relative);
      if (fs.existsSync(target)) {
        const stat = fs.lstatSync(target);
        if (!stat.isFile() || stat.isSymbolicLink() || sha(fs.readFileSync(target)) !== expected) throw Error('Refusing to replace unknown existing community artifact');
      } else {
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.copyFileSync(path.join(vendor, plugin.directory, relative), target, fs.constants.COPYFILE_EXCL);
      }
    }
    const guardedEntry = path.join(destination, 'megmeet-guarded-host.mjs');
    if (fs.existsSync(guardedEntry) && (!fs.lstatSync(guardedEntry).isFile() || fs.lstatSync(guardedEntry).isSymbolicLink())) {
      throw Error('Community guarded entry must be a regular file');
    }
    fs.writeFileSync(guardedEntry, guard);
  }
  const patch = [{ insert: manifest.plugins.map(p => ({ id: 'dshp-' + p.directory, name: p.name, config: COMMUNITY_DEFAULTS[p.directory] })) }];
  const patchPath = path.join(runtime, 'community.patch.json');
  fs.writeFileSync(patchPath, JSON.stringify(patch, null, 2) + '\n');
  return { commit: manifest.commit, manifestSha256: manifest.manifestSha256,
    plugins: manifest.plugins.map(({ name, version }) => ({ name, version, guardSha256 })),
    patchPath, patchSha256: sha(fs.readFileSync(patchPath)) };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (!process.argv[2]) throw Error('Provide a candidate runtime directory; no download or install is performed automatically');
  console.log(JSON.stringify(stageCommunity(process.argv[2]), null, 2));
}
