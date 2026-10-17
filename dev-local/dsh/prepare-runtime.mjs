// Verify the entire official installation, not only the CLI version label.
// Enterprise UI adaptations are applied before hashing the served resources.
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { stageCommunity, verifyCommunitySource } from './community-plugins.mjs';

const expected = '0.1.6-alpha.2';
const root = process.argv[2] ?? '/opt/dsh/node_modules/@deepseek-ai';
const metadataDir = process.argv[3] ?? '/opt/megmeet';
const sha = value => createHash('sha256').update(value).digest('hex');
const community = stageCommunity(path.resolve(root, '../..'));
const communitySource = verifyCommunitySource();
delete communitySource.manifestSha256;
writeFileSync(path.join(metadataDir, 'community-manifest.json'), JSON.stringify(communitySource, null, 2) + '\n');
writeFileSync(path.join(metadataDir, 'community.patch.json'), readFileSync(community.patchPath));
const packages = {};
const resources = {};
for (const name of readdirSync(root).sort()) {
  if (name !== 'dsh' && !name.startsWith('dsh-')) continue;
  const dir = path.join(root, name);
  const pkg = JSON.parse(readFileSync(path.join(dir, 'package.json'), 'utf8'));
  if (pkg.version !== expected) throw Error(`Mixed DSH release: ${name}@${pkg.version}`);
  packages[pkg.name] = pkg.version;
}
for (const name of ['dsh', 'dsh-web-frontend', 'dsh-web-app', 'dsh-client-ui-conversation', 'dsh-client-ui-settings-models']) {
  if (!packages['@deepseek-ai/' + name]) throw Error('Missing release component: ' + name);
}

const noticePath = path.join(root, 'dsh-client-ui-settings-models/lib/client.js');
const original = readFileSync(noticePath, 'utf8');
const originalHash = sha(original);
if (originalHash !== '75ad987e7ca4de05df72bab31b8f6e9277f3946e122aa6c0b17031b875170ab6') {
  throw Error('Review welcome-notice adaptation for this upstream release');
}
const welcome = /ctx\.slots\.inject\("settings\.onboarding", \(\) => ctx\.slots\.register\(\{\s*name: "settings\.onboarding",\s*id: "welcome-notice",\s*order: -100,\s*inject: welcomeInjected\s*\}, WelcomeNotice\)\);/g;
if ([...original.matchAll(welcome)].length !== 1) throw Error('Welcome registration contract differs');
writeFileSync(noticePath, original.replace(welcome, '/* Enterprise preference: internal-testing notice disabled. */'));

for (const name of Object.keys(packages)) {
  const dir = path.join(root, name.slice('@deepseek-ai/'.length));
  const clientFile = path.join(dir, 'lib/client.js');
  try {
    // Upstream serves versioned combo URLs, stripping local debug trailers and
    // adding a semicolon before its new source-map URL. Hash executable bytes.
    let source = readFileSync(clientFile, 'utf8')
      .replace(/(?:\r?\n)?\/\/# sourceURL=[^\r\n]*(?:\r?\n)?$/, '')
      .replace(/(?:\r?\n)?\/\/# sourceMappingURL=[^\r\n]*(?:\r?\n)?$/, '');
    if (!source.endsWith('\n')) source += '\n';
    resources['/plugins/' + name + '/client.js'] = sha(source + ';\n');
  }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const frontend = path.join(root, 'dsh-web-frontend/dist');
function staticFiles(dir, relative = '') {
  for (const item of readdirSync(dir, { withFileTypes: true })) {
    const next = path.posix.join(relative, item.name);
    if (item.isDirectory()) staticFiles(path.join(dir, item.name), next);
    else if (next.endsWith('.js') || next.endsWith('.css')) {
      resources['/' + next] = sha(readFileSync(path.join(dir, item.name)));
    }
  }
}
staticFiles(frontend);
const branding = JSON.parse(readFileSync(path.join(metadataDir, 'brand-adaptation.json'), 'utf8'));
if (branding.dsh !== expected || branding.branding !== 'megmeet-assets-r5') {
  throw Error('Brand adaptation release metadata differs');
}
const manifest = {
  dsh: expected, frontend: packages['@deepseek-ai/dsh-web-frontend'],
  packages, resources,
  adaptations: [{ name: 'hide-internal-testing-notice', package: '@deepseek-ai/dsh-client-ui-settings-models', originalSha256: originalHash }, ...branding.adaptations],
  branding: branding.branding,
  brandAssets: branding.assets,
  enterpriseAdapter: branding.enterpriseAdapter,
  workspaceAdapter: branding.workspaceAdapter,
  community: { commit: community.commit, plugins: community.plugins, patchSha256: community.patchSha256,
    manifestSha256: sha(readFileSync(path.join(metadataDir, 'community-manifest.json'))) },
};
manifest.releaseId = sha(JSON.stringify(manifest));
writeFileSync(path.join(metadataDir, 'release-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`DSH_RELEASE_COHERENT: ${Object.keys(packages).length} packages @ ${expected}`);
console.log('FRONTEND: native DSH with MEGMEET images/title; Preview and internal-testing notice disabled');
