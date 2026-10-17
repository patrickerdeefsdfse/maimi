'use strict';
// Canonical desktop boundary. scripts/verify-dsh-candidate.cjs synchronizes the
// identical app-local copy; packaged code never resolves outside app.asar.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const VERSION = '0.1.6-alpha.2';
const ADAPTER = 'megmeet-dsh-native-v1';
const NODE = '24.13.0';
const NODE_SHA256 = 'd14ba95cdce1ef7dc9ad3ac74949ca5db38b27378ee30f30a23cf26f9e875a11';
const DATA_FORMAT = 'dsh-0.1.6-alpha.1-native';
const CONTRACTS = Object.freeze({ nativeRpc: 'dsh-named-rpc-0.1.6-alpha.2',
  workspace: 'megmeet-personal-cloud-v1', enterprise: 'megmeet-plugins-v1',
  personalModel: 'openai-chat-completions-v1', data: DATA_FORMAT });
const CORE = ['dsh', 'dsh-web-frontend', 'dsh-web-app', 'dsh-agent',
  'dsh-api-workspace-controller', 'dsh-api-session-controller',
  'dsh-client-connection', 'dsh-client-ui-conversation', 'dsh-client-ui-brand-official',
  'dsh-client-ui-settings-models', 'dsh-llm-deepseek'];
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
    nativeData: { read: [DATA_FORMAT], write: DATA_FORMAT, migration: 'none' },
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
    && set.extension.version === '3.3.0', 'DSH_EXTENSION_REQUIRED');
  check(set.community?.commit === 'b9ea3d7625a7ec40cf191094e7b83571ba5c0a79'
    && set.community.plugins?.length === 4, 'DSH_COMMUNITY_REVIEW_REQUIRED');
  check(set.extension.workspace?.version === '1.0.0'
    && /^[0-9a-f]{64}$/.test(set.extension.workspace.sha256), 'DSH_WORKSPACE_EXTENSION_REQUIRED');
  check(JSON.stringify(set.contracts) === JSON.stringify(CONTRACTS), 'DSH_CONTRACT_UNSUPPORTED');
  check(set.nativeData?.write === DATA_FORMAT && set.nativeData.migration === 'none'
    && Array.isArray(set.nativeData.read) && set.nativeData.read.length === 1
    && set.nativeData.read[0] === DATA_FORMAT, 'DSH_DATA_MIGRATION_REQUIRED');
  check(set.packages && Object.keys(set.packages).length === 250
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
    && release.workspaceAdapter?.version === '1.0.0'
    && digestFile(path.join(root, 'workspace-pinned-overlay.mjs')) === release.workspaceAdapter?.sha256, 'DSH_WORKSPACE_EXTENSION_INTEGRITY');
  const adapterSha256 = digestFile(__filename);
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
  check((brand.adaptations || []).length === 3, 'DSH_ADAPTATION_REQUIRED');
  check(JSON.stringify(brand.adaptations.map(item => item.package).sort()) === JSON.stringify([
    '@deepseek-ai/dsh-client-ui-brand-official', '@deepseek-ai/dsh-client-ui-conversation', '@deepseek-ai/dsh-client-ui-workspace'].sort()), 'DSH_ADAPTATION_REQUIRED');
  check(brand.adaptations.some(item => item.package === '@deepseek-ai/dsh-client-ui-workspace'
    && item.originalSha256 === 'cec5cdfc90b0732356b678e84ee00949d26b8c1b70d4dd5fcc20660d7267e448'), 'DSH_WORKSPACE_INPUT_CONTRACT_REQUIRED');
  check(release.adaptations?.some(item => item.name === 'hide-internal-testing-notice'
    && item.package === '@deepseek-ai/dsh-client-ui-settings-models'
    && item.originalSha256 === '75ad987e7ca4de05df72bab31b8f6e9277f3946e122aa6c0b17031b875170ab6'), 'DSH_NOTICE_ADAPTATION_REQUIRED');
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
function modelPatch(catalog, origin) {
  return [
    { id: 'agent-default-model', config: { provider: 'deepseek-official', model: catalog.defaultModel || 'deepseek-flash-v4.1' } },
    { id: 'llm-deepseek', config: { protocol: 'chat-completions', apiKeyEnv: 'DEEPSEEK_API_KEY',
      baseURL: origin + '/__megmeet/model/v1', models: catalog.models } },
  ];
}
function modelSettingsUpdates(catalog, origin) {
  return modelPatch(catalog, origin).filter(item => item.id !== 'agent-default-model' || catalog.defaultModel).reverse().map(item => ({ method: 'settings/update',
    args: { ns: item.id, patch: item.config } }));
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
  }
  return patch;
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
function guardDataFormat(home) {
  const marker = path.join(home, 'megmeet-native-data.json');
  if (fs.existsSync(marker)) {
    const state = json(marker);
    check(state.schema === 1 && state.format === DATA_FORMAT, 'DSH_DATA_MIGRATION_REQUIRED');
    return;
  }
  // The only supported legacy runtime here is the already deployed exact
  // 0.1.6-alpha.1 build. Never migrate or rewrite native session files.
  fs.writeFileSync(marker, JSON.stringify({ schema: 1, format: DATA_FORMAT }) + '\n', { flag: 'wx' });
}
module.exports = { VERSION, ADAPTER, NODE, DATA_FORMAT, CONTRACTS, sha, componentSet,
  assertSupportedSet, validateRuntime, modelPatch, modelSettingsUpdates, communityPatch, launchArguments, startupToken, guardDataFormat };
