'use strict';
// Canonical desktop boundary. scripts/verify-dsh-candidate.cjs synchronizes the
// identical app-local copy; packaged code never resolves outside app.asar.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const VERSION = '0.2.0-rc.2';
const PACKAGE_SET_SHA256 = '43333d9f25c9b208cd240f27ab3048678af7c12f97c767f2dc4c821e24f034cf';
const ADAPTER = 'megmeet-dsh-native-v1';
const NODE = '24.13.0';
const NODE_SHA256 = 'd14ba95cdce1ef7dc9ad3ac74949ca5db38b27378ee30f30a23cf26f9e875a11';
const LEGACY_DATA_FORMAT = 'dsh-0.1.6-alpha.1-native';
const DATA_FORMAT = 'dsh-0.1.7-alpha.2-native-v4';
const MIGRATION = 'offline-owned-v4-020-admission-v1';
const { pathToFileURL } = require('node:url');
const HELPERS = {"enterprise-model-policy.mjs":"b0ffb8b31f5a7f34ac9c41782e746db9a725a207286155575f0d3f0bb1a5e33a","catalog.mjs":"68a852b7b422fb355d9cd3a509ed386637b0520a33d0f490d27f55022d031860","managed-profile.mjs":"2c2dcebb63717b77f91d434accd5e67e069d9688b5f1979277355ef63c2e6bc6","history-migration.mjs":"7f6951a575cfcba356114468ea838a25c3598145fae9fbf407e663e17d2e68e0","history-native.mjs":"5cd1e2493ca5a0d4e56a213f65560924afb100de9b86b6d510b60c4f4a0d9c1c","history-runtime-snapshot.mjs":"529b0afb10e68148b99501f74759f00532fabba4c17eedb002ceb0aa862bb107","community-profile.mjs":"715208ee35485c768369b65cebd4b7a0573c23e59067d532deb8ba6052cbf9d4","history-admission-rc2.mjs":"040363eb4ddc66201ccb1f530515293fbff9fe9b720319c1ea8d1b1044284094","enterprise-profile.mjs":"d68e680e65abf01ae346480f0de60f5309a441ee6f265bd42dbf1b55e39ab43e"};
function verifiedHelper(name) {
  check(Object.hasOwn(HELPERS,name) && digestFile(path.join(__dirname,name)) === HELPERS[name], 'DSH_ADAPTER_HELPER_INTEGRITY');
  return path.join(__dirname,name);
}
const normalizeCatalog = (...args) => require(verifiedHelper('catalog.mjs')).normalizeCatalog(...args);
const providerPatch = (...args) => require(verifiedHelper('catalog.mjs')).providerPatch(...args);

const CONTRACTS = Object.freeze({ nativeRpc: 'dsh-named-rpc-0.2.0-rc.2',
  workspace: 'megmeet-personal-cloud-v1', enterprise: 'megmeet-plugins-v1',
  personalModel: 'openai-chat-completions-v1', data: DATA_FORMAT });
const CORE = ['dsh', 'dsh-web-frontend', 'dsh-web-app', 'dsh-agent',
  'dsh-api-workspace-controller', 'dsh-api-session-controller',
  'dsh-client-connection', 'dsh-client-ui-conversation', 'dsh-client-ui-brand-official',
  'dsh-client-ui-settings-models', 'dsh-llm-pi-ai', 'dsh-session-format-v3-to-v4', 'dsh-session-format-catalog'];
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const json = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const digestFile = file => sha(fs.readFileSync(file));
function check(condition, code) { if (!condition) throw Object.assign(Error(code), { code }); }
function safeRelative(root, relative) {
  check(typeof relative === 'string' && relative.length > 0 && !relative.includes('\\')
    && !relative.startsWith('/') && relative.split('/').every(v => v && v !== '.' && v !== '..'), 'DSH_UNSAFE_RESOURCE_PATH');
  const target = path.join(root, ...relative.split('/'));
  const real = fs.realpathSync(target), base = fs.realpathSync(root);
  check(real.startsWith(base + path.sep), 'DSH_RESOURCE_OUTSIDE_RUNTIME');
  return target;
}
function componentSet(release, manifest, adapterSha256) {
  return {
    schema: 1, adapter: ADAPTER, adapterSha256,
    runtime: VERSION, frontend: VERSION, sdk: { kind: 'npm-native-rpc', version: VERSION },
    node: NODE, platform: 'win32-x64', packages: release.packages,
    extension: { branding: release.branding, version: release.enterpriseAdapter.version,
      sha256: release.enterpriseAdapter.sha256, workspace: release.workspaceAdapter }, contracts: CONTRACTS,
    community: release.community,
    nativeData: { read: [LEGACY_DATA_FORMAT, DATA_FORMAT], write: DATA_FORMAT, migration: MIGRATION },
    releaseId: release.releaseId, releaseSha256: manifest.releaseSha256,
    adaptationSha256: manifest.adaptationSha256, npmLockSha256: manifest.npmLockSha256,
    verification: 'static-release-integrity-only',
  };
}
function assertSupportedSet(set) {
  check(set && set.schema === 1 && set.adapter === ADAPTER, 'DSH_ADAPTER_UNSUPPORTED');
  check(set.runtime === VERSION && set.frontend === VERSION
    && set.sdk?.kind === 'npm-native-rpc' && set.sdk?.version === VERSION
    && set.node === NODE && set.platform === 'win32-x64', 'DSH_MIXED_RELEASE');
  check(set.extension?.branding === 'megmeet-assets-r5'
    && /^[0-9a-f]{64}$/.test(set.extension.sha256)
    && set.extension.version === '3.7.0', 'DSH_EXTENSION_REQUIRED');
  check(set.community?.commit === 'b9ea3d7625a7ec40cf191094e7b83571ba5c0a79'
    && set.community.plugins?.length === 4, 'DSH_COMMUNITY_REVIEW_REQUIRED');
  check(set.extension.workspace?.version === '2.2.0'
    && /^[0-9a-f]{64}$/.test(set.extension.workspace.sha256), 'DSH_WORKSPACE_EXTENSION_REQUIRED');
  check(JSON.stringify(set.contracts) === JSON.stringify(CONTRACTS), 'DSH_CONTRACT_UNSUPPORTED');
  check(set.nativeData?.write === DATA_FORMAT && set.nativeData.migration === MIGRATION
    && JSON.stringify(set.nativeData.read) === JSON.stringify([LEGACY_DATA_FORMAT, DATA_FORMAT]), 'DSH_DATA_MIGRATION_REQUIRED');
  check(set.packages && sha(JSON.stringify(Object.keys(set.packages).sort().map(k => [k,set.packages[k]]))) === PACKAGE_SET_SHA256
    && Object.entries(set.packages).every(([key, version]) => /^@deepseek-ai\/dsh(?:-|$)/.test(key) && version === VERSION)
    && CORE.every(name => set.packages['@deepseek-ai/' + name] === VERSION), 'DSH_MIXED_PACKAGE_SET');
  return set;
}
function validateRuntime(root, { full = false, requireCompatibility = true } = {}) {
  const manifest = json(path.join(root, 'runtime-manifest.json'));
  check(manifest.dsh === VERSION && manifest.node === NODE && manifest.platform === 'win32-x64', 'DSH_RUNTIME_UNSUPPORTED');
  const node = path.join(root, 'bin/node.exe');
  check(manifest.nodeSha256 === NODE_SHA256 && digestFile(node) === NODE_SHA256, 'DSH_NODE_INTEGRITY');
  for (const [file, field] of [['package-lock.json', 'npmLockSha256'], ['release-manifest.json', 'releaseSha256'], ['brand-adaptation.json', 'adaptationSha256']])
    check(digestFile(path.join(root, file)) === manifest[field], 'DSH_MANIFEST_INTEGRITY');
  const release = json(path.join(root, 'release-manifest.json'));
  check(digestFile(path.join(root, 'community.patch.json')) === release.community?.patchSha256
    && digestFile(path.join(root, 'community-manifest.json')) === release.community?.manifestSha256, 'DSH_COMMUNITY_INTEGRITY');
  const community = json(path.join(root, 'community-manifest.json'));
  check(community.commit === release.community.commit && community.dsh === VERSION
    && JSON.stringify(community.plugins.map(({ name, version }) => ({ name, version }))) === JSON.stringify(release.community.plugins.map(({ name, version }) => ({ name, version }))), 'DSH_COMMUNITY_VERSION');
  for (const plugin of community.plugins) {
    check(/^@dshp\/(?:file-change-viewer|web-style|widget-kit|token-meter)$/.test(plugin.name), 'DSH_COMMUNITY_NOT_REVIEWED');
    const guard = release.community.plugins.find(item => item.name === plugin.name)?.guardSha256;
    check(/^[a-f0-9]{64}$/.test(guard || '') && digestFile(safeRelative(path.join(root, 'node_modules'), plugin.name + '/megmeet-guarded-host.mjs')) === guard, 'DSH_COMMUNITY_AUTH_GUARD_REQUIRED');
    for (const [relative, digest] of Object.entries(plugin.files))
      check(digestFile(safeRelative(path.join(root, 'node_modules'), plugin.name + '/' + relative)) === digest, 'DSH_COMMUNITY_FILE_INTEGRITY');
  }
  const brand = json(path.join(root, 'brand-adaptation.json'));
  const { releaseId, ...releaseBody } = release;
  check(sha(JSON.stringify(releaseBody)) === releaseId, 'DSH_RELEASE_ID_INTEGRITY');
  check(release.dsh === VERSION && release.frontend === VERSION && brand.dsh === VERSION, 'DSH_MIXED_RELEASE');
  check(JSON.stringify(release.enterpriseAdapter) === JSON.stringify(brand.enterpriseAdapter)
    && digestFile(path.join(root, 'enterprise-native.mjs')) === release.enterpriseAdapter?.sha256, 'DSH_EXTENSION_INTEGRITY');
  check(JSON.stringify(release.workspaceAdapter) === JSON.stringify(brand.workspaceAdapter)
    && release.workspaceAdapter?.version === '2.2.0'
    && digestFile(path.join(root, 'workspace-pinned-overlay.mjs')) === release.workspaceAdapter?.sha256, 'DSH_WORKSPACE_EXTENSION_INTEGRITY');
  check(release.reviewSkills?.version === '1.0.0' && Object.keys(release.reviewSkills.files || {}).length >= 4, 'DSH_REVIEW_SKILLS_REQUIRED');
  for (const [relative, digest] of Object.entries(release.reviewSkills.files)) {
    const file = safeRelative(path.join(root, 'review-skills'), relative);
    check(!fs.lstatSync(file).isSymbolicLink() && digestFile(file) === digest, 'DSH_REVIEW_SKILLS_INTEGRITY');
  }
  const adapterSha256 = digestFile(__filename);
  check(release.logisticsSkill?.version === '1.0.0' && Object.keys(release.logisticsSkill.files || {}).length >= 3, 'DSH_LOGISTICS_SKILL_REQUIRED');
  for (const [relative, digest] of Object.entries(release.logisticsSkill.files)) {
    const file = safeRelative(path.join(root, 'logistics-skill'), relative);
    check(!fs.lstatSync(file).isSymbolicLink() && digestFile(file) === digest, 'DSH_LOGISTICS_SKILL_INTEGRITY');
  }
  const supported = assertSupportedSet(componentSet(release, manifest, adapterSha256));
  const scoped = path.join(root, 'node_modules/@deepseek-ai');
  const names = fs.readdirSync(scoped).filter(name => name === 'dsh' || name.startsWith('dsh-')).sort();
  check(JSON.stringify(names.map(name => '@deepseek-ai/' + name)) === JSON.stringify(Object.keys(release.packages).sort()), 'DSH_PACKAGE_INVENTORY_MISMATCH');
  for (const name of names) {
    const pkg = json(path.join(scoped, name, 'package.json'));
    check(pkg.name === '@deepseek-ai/' + name && pkg.version === VERSION, 'DSH_INSTALLED_PACKAGE_MISMATCH');
  }
  for (const item of brand.adaptations || []) {
    check(release.packages[item.package] === VERSION, 'DSH_ADAPTATION_PACKAGE_UNKNOWN');
    check(digestFile(path.join(root, 'node_modules', item.package, 'lib/client.js')) === item.adaptedSha256, 'DSH_ADAPTATION_INTEGRITY');
  }
  check((brand.adaptations || []).length === 5, 'DSH_ADAPTATION_REQUIRED');
  check(JSON.stringify(brand.adaptations.map(item => item.package).sort()) === JSON.stringify([
    '@deepseek-ai/dsh-client-ui-brand-official', '@deepseek-ai/dsh-client-ui-conversation', '@deepseek-ai/dsh-client-ui-workspace', '@deepseek-ai/dsh-client-ui-settings-models', '@deepseek-ai/dsh-client-ui-chat'].sort()), 'DSH_ADAPTATION_REQUIRED');
  check(brand.adaptations.some(item => item.package === '@deepseek-ai/dsh-client-ui-workspace'
    && item.originalSha256 === 'e784543780fb8ac7c38c853afd26b9b1713c6907eec2b07eff37461e07bd3be9'), 'DSH_WORKSPACE_INPUT_CONTRACT_REQUIRED');
  check(release.adaptations?.some(item => item.name === 'megmeet-020-compat'
    && item.package === '@deepseek-ai/dsh-client-ui-settings-models'
    && item.originalSha256 === '7674ed0ba2eef60fa7db91b4bf8cea4dc23e1f0a2f3dd1e6a2a67ad4c0d005fa'), 'DSH_NOTICE_ADAPTATION_REQUIRED');
  for (const [name, digest] of Object.entries(brand.assets || {}))
    check(digestFile(safeRelative(path.join(root, 'assets'), name)) === digest, 'DSH_ASSET_INTEGRITY');
  if (full) {
    check(release.resources && Object.keys(release.resources).length > 0, 'DSH_RESOURCES_REQUIRED');
    for (const [url, digest] of Object.entries(release.resources)) {
      let bytes;
      if (url.startsWith('/plugins/@deepseek-ai/')) {
        const relative = url.slice('/plugins/'.length);
        const file = safeRelative(path.join(root, 'node_modules'), relative.replace(/\/client\.js$/, '/lib/client.js'));
        let source = fs.readFileSync(file, 'utf8')
          .replace(/(?:\r?\n)?\/\/# sourceURL=[^\r\n]*(?:\r?\n)?$/, '')
          .replace(/(?:\r?\n)?\/\/# sourceMappingURL=[^\r\n]*(?:\r?\n)?$/, '');
        if (!source.endsWith('\n')) source += '\n';
        bytes = source + ';\n';
      } else bytes = fs.readFileSync(safeRelative(path.join(scoped, 'dsh-web-frontend/dist'), url.slice(1)));
      check(sha(bytes) === digest, 'DSH_SERVED_RESOURCE_INTEGRITY');
    }
  }
  if (requireCompatibility) {
    const file = path.join(root, 'compatibility-manifest.json');
    check(fs.existsSync(file) && digestFile(file) === manifest.compatibilitySha256, 'DSH_COMPATIBILITY_MANIFEST_REQUIRED');
    const compatibility = json(file);
    assertSupportedSet(compatibility);
    check(JSON.stringify(compatibility) === JSON.stringify(supported), 'DSH_COMPATIBILITY_SET_MISMATCH');
  }
  return { root, node, manifest, release, compatibility: supported };
}
function managedCatalog(catalog) {
  return normalizeCatalog({data:catalog.models,default_model:catalog.defaultModel});
}
function modelPatch() {
  // Dynamic catalog/default live in the editable web profile. Putting them in
  // this CLI layer makes the new native settings API reject later updates.
  return require(verifiedHelper('enterprise-profile.mjs')).enterpriseProfile({cloud:false});
}
function modelSettingsUpdates(catalog, origin) {
  const next=managedCatalog(catalog);
  const updates=[{method:'settings/replace',args:{ns:'llm-pi-ai',section:providerPatch(next,origin+'/__megmeet/model/v1')}}];
  if(next.data.length)updates.push({method:'settings/update',args:{ns:'agent-default-model',patch:{provider:'deepseek-official',model:next.default_model}}});
  return updates;
}
async function prepareModelProfile(home,runtimeRoot,catalog,origin) {
  const {prepareManagedProfile}=await import(pathToFileURL(verifiedHelper('managed-profile.mjs')));
  const yaml=require(path.join(runtimeRoot,'node_modules/yaml'));
  const next=managedCatalog(catalog);
  await prepareManagedProfile({home,profile:'web',catalogConfig:providerPatch(next,origin+'/__megmeet/model/v1'),
    defaultConfig:{provider:'deepseek-official',model:next.default_model||'megmeet-unavailable'},yaml});
}
async function prepareDesktopCommunityProfile(home,runtimeRoot) {
  communityPatch(runtimeRoot); // Validate the exact fixed patch and guards.
  const {prepareCommunityProfile}=await import(pathToFileURL(verifiedHelper('community-profile.mjs')));
  const yaml=require(path.join(runtimeRoot,'node_modules/yaml'));
  return prepareCommunityProfile({home,runtimeRoot,yaml,profile:'web'});
}
function communityPatch(root) {
  const release = json(path.join(root, 'release-manifest.json'));
  const source = path.join(root, 'community.patch.json');
  check(digestFile(source) === release.community?.patchSha256, 'DSH_COMMUNITY_INTEGRITY');
  const patch = json(source);
  check(patch.length === 1 && patch[0].insert?.length === 4, 'DSH_COMMUNITY_PATCH_INVALID');
  for (const plugin of patch[0].insert) {
    check(/^@dshp\/(?:file-change-viewer|web-style|widget-kit|token-meter)$/.test(plugin.name), 'DSH_COMMUNITY_NOT_REVIEWED');
    const guard = release.community.plugins.find(item => item.name === plugin.name)?.guardSha256;
    plugin.name = safeRelative(path.join(root, 'node_modules'), plugin.name + '/megmeet-guarded-host.mjs');
    check(digestFile(plugin.name) === guard, 'DSH_COMMUNITY_AUTH_GUARD_REQUIRED');
    plugin.name = pathToFileURL(plugin.name).href;
  }
  // The reviewed insert entries now live in the writable profile layer.
  // Keep a CLI patch file for the existing launch contract, containing [].
  return [];
}
function launchArguments(root, { patch, community, port, authority }) {
  check(Number.isInteger(port) && port > 0 && port <= 65535 && /^127\.0\.0\.1:\d+$/.test(authority), 'DSH_LAUNCH_ARGUMENTS_INVALID');
  check(typeof patch === 'string' && patch.length > 0 && typeof community === 'string' && community.length > 0, 'DSH_LAUNCH_PATCH_REQUIRED');
  return ['--expose-internals', path.join(root, 'node_modules/@deepseek-ai/dsh/lib/bin.js'),
    '--profile', 'web', '--patch', community, '--patch', patch,
    '--host', '127.0.0.1', '--port', String(port), '--no-open', '--trusted-host', authority];
}
function startupToken(startup, port) {
  check(Number.isInteger(port) && port > 0 && port <= 65535, 'DSH_LAUNCH_ARGUMENTS_INVALID');
  return startup.match(new RegExp(`http://127\\.0\\.0\\.1:${port}/\\?token=([A-Za-z0-9_-]+)`))?.[1];
}
async function guardDataFormat(home, {runtimeRoot,assertQuiescent}={}) {
  check(path.basename(home)==='dsh' && typeof runtimeRoot==='string' && typeof assertQuiescent==='function','DSH_OFFLINE_MIGRATION_REQUIRED');
  const marker=path.join(home,'megmeet-native-data.json');
  if(fs.existsSync(marker)) {
    check(!fs.lstatSync(marker).isSymbolicLink(),'DSH_DATA_MARKER_UNSAFE');
    const state=json(marker);
    check(state.schema===1 && [LEGACY_DATA_FORMAT,DATA_FORMAT].includes(state.format),'DSH_DATA_MIGRATION_REQUIRED');
  }
  verifiedHelper('history-native.mjs');
  verifiedHelper('history-migration.mjs');
  verifiedHelper('history-runtime-snapshot.mjs');
  const {createHistoryMigration}=await import(pathToFileURL(verifiedHelper('history-admission-rc2.mjs')));
  await createHistoryMigration({stateRoot:path.dirname(home),runtimeRoot,assertQuiescent}).admit();
  // Keep the prior app marker as evidence, and write V4 only after migration
  // publishes and verifies a complete native receipt. Never label old logs V4.
  if(fs.existsSync(marker)&&json(marker).format!==DATA_FORMAT) {
    try{fs.copyFileSync(marker,marker+'.before-dsh17',fs.constants.COPYFILE_EXCL);}catch(error){if(error.code!=='EEXIST')throw error;}
  }
  const temporary=marker+'.'+crypto.randomUUID()+'.tmp';
  fs.writeFileSync(temporary,JSON.stringify({schema:1,format:DATA_FORMAT,migration:MIGRATION})+'\n',{flag:'wx'});
  fs.renameSync(temporary,marker);
}

function modelPolicyPatch(home, modelBase) {
  return require('./model-policy-profile.cjs').modelPolicyPatch(home, modelBase, verifiedHelper('enterprise-model-policy.mjs'));
}
module.exports = { modelPolicyPatch, VERSION, ADAPTER, NODE, DATA_FORMAT, CONTRACTS, sha, componentSet,
  assertSupportedSet, validateRuntime, modelPatch, modelSettingsUpdates, communityPatch, launchArguments, startupToken, guardDataFormat, prepareModelProfile, prepareDesktopCommunityProfile, LEGACY_DATA_FORMAT, MIGRATION };
