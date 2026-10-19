import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export const TARGET = Object.freeze({ version: '0.2.1-rc.2', commit: '687f282e8625c6b8d11ff58211a90524be2e4317', pnpm: '11.28.5', sourceArchiveSha256: '157903b22c2881d3678d846755afe957a48df834e747deda805a1195ce25a146' });
export const sha256 = data => crypto.createHash('sha256').update(data).digest('hex');
const fail = code => { throw new Error(code); };
const record = x => x && typeof x === 'object' && !Array.isArray(x);

export function regularFile(root, relative) {
  if (typeof relative !== 'string' || !relative || relative.includes('\\') || relative.startsWith('/') || relative.split('/').some(p => !p || p === '.' || p === '..')) fail('UNSAFE_SOURCE_PATH');
  let file = path.resolve(root);
  if (fs.lstatSync(file).isSymbolicLink()) fail('LINKED_SOURCE_ROOT');
  for (const item of relative.split('/')) {
    file = path.join(file, item);
    if (fs.lstatSync(file).isSymbolicLink()) fail('LINKED_SOURCE_MEMBER');
  }
  if (!fs.lstatSync(file).isFile()) fail('SOURCE_NOT_REGULAR_FILE');
  return file;
}

export function readJson(root, relative) { return JSON.parse(fs.readFileSync(regularFile(root, relative), 'utf8')); }

export function inspectSource(source, pins) {
  if (!record(pins) || pins.schema !== 'maimi-dsh-native-desktop-source-lock-v1' || pins.version !== TARGET.version || pins.commit !== TARGET.commit || pins.source_archive_sha256 !== TARGET.sourceArchiveSha256 || !Array.isArray(pins.files) || pins.files.length < 10) fail('DESKTOP_SOURCE_LOCK_INVALID');
  const root = readJson(source, 'package.json');
  const desktop = readJson(source, 'apps/desktop/package.json');
  const host = readJson(source, 'apps/desktop-host/package.json');
  const cli = readJson(source, 'apps/cli/package.json');
  if ([root, desktop, host, cli].some(p => p.version !== TARGET.version) || desktop.name !== '@deepseek-ai/dsh-desktop' || root.packageManager !== `pnpm@${TARGET.pnpm}`) fail('DESKTOP_VERSION_SET_MISMATCH');
  if (root.scripts?.['build:artifacts'] !== 'tsx scripts/build.ts --scope artifacts' || desktop.scripts?.['package:win:x64:unsigned'] !== 'tsx scripts/package-target.ts win-x64 --unsigned') fail('NATIVE_BUILD_ENTRY_CHANGED');
  const names = new Set();
  for (const item of pins.files) {
    if (!record(item) || names.has(item.path) || !/^[a-f0-9]{64}$/.test(item.sha256) || !Number.isSafeInteger(item.bytes)) fail('DESKTOP_SOURCE_LOCK_INVALID');
    names.add(item.path);
    const bytes = fs.readFileSync(regularFile(source, item.path));
    if (bytes.length !== item.bytes || sha256(bytes) !== item.sha256) fail('DESKTOP_SOURCE_HASH_MISMATCH');
  }
  for (const required of ['package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'apps/desktop/package.json', 'apps/desktop/src/main.ts', 'apps/desktop/src/host-process.ts', 'apps/desktop-host/src/index.ts']) if (!names.has(required)) fail('DESKTOP_SOURCE_LOCK_INCOMPLETE');
  return { schema: 'maimi-dsh-native-desktop-source-check-v1', status: 'VERIFIED', version: TARGET.version, commit: TARGET.commit, pinned_files: pins.files.length, upstream_ui_overlays: 0, company_requests: 0 };
}

export function makeBuildPlan({ source, stage, state, pnpmEntry }) {
  const resolved = [source, stage, state, pnpmEntry].map(v => {
    if (typeof v !== 'string' || !path.isAbsolute(v)) fail('ABSOLUTE_BUILD_PATH_REQUIRED');
    return path.resolve(v);
  });
  const [sourceRoot, stageRoot, stateRoot, pnpm] = resolved;
  const nested = (parent, child) => { const rel = path.relative(parent, child); return rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel)); };
  if (nested(sourceRoot, stageRoot) || nested(stageRoot, sourceRoot) || nested(sourceRoot, stateRoot) || nested(stateRoot, sourceRoot) || nested(stageRoot, stateRoot) || nested(stateRoot, stageRoot)) fail('BUILD_PATHS_OVERLAP');
  const invocation = (...args) => ({ executable: process.execPath, args: [pnpm, ...args], cwd: stageRoot, shell: false, env: { DSH_CLIENT_COMMIT_HASH: TARGET.commit, DSH_CLIENT_TITLE: '麦米 DSH 升级候选' } });
  return {
    schema: 'maimi-dsh-native-desktop-build-plan-v1', target: TARGET,
    source: sourceRoot, stage: stageRoot, state: stateRoot,
    commands: [invocation('install', '--frozen-lockfile', '--ignore-scripts'), invocation('--filter', '@deepseek-ai/dsh-desktop', 'exec', 'install-electron'), invocation('run', 'build:artifacts'), invocation('--filter', '@deepseek-ai/dsh-desktop', 'run', 'build'), invocation('--filter', '@deepseek-ai/dsh-desktop', 'run', 'package:win:x64:unsigned')],
    launch_environment: { DSH_HOME: path.join(stateRoot, 'home'), DSH_DESKTOP_USER_DATA_DIR: path.join(stateRoot, 'electron-user-data'), DSH_DESKTOP_OPEN_DEVTOOLS: '0' },
    source_overlay_allowlist: [],
    company_publish_allowed: false,
    launch_allowed: false,
    blockers: ['COMPANY_IAM_NATIVE_ONBOARDING_NOT_INTEGRATED', 'COMPANY_MODEL_AUTHORIZATION_NOT_VERIFIED', 'ENTERPRISE_LOCAL_SESSION_FILE_BRIDGE_NOT_VERIFIED', 'COMPANY_UPDATE_TRUST_NOT_INTEGRATED', 'NATIVE_HISTORY_MIGRATION_NOT_VERIFIED'],
  };
}

// This copies an independently pinned public source tree to a new build folder.
// No cleanup, old installation access, or update-feed operation is provided.
export function stageSource(source, destination, pins) {
  const checked = inspectSource(source, pins);
  if (fs.existsSync(destination)) fail('STAGE_ALREADY_EXISTS');
  const sourceReal = fs.realpathSync(source);
  const parentReal = fs.realpathSync(path.dirname(destination));
  const destReal = path.join(parentReal, path.basename(destination));
  const rel = path.relative(sourceReal, destReal);
  if (rel === '' || (!rel.startsWith('..' + path.sep) && rel !== '..' && !path.isAbsolute(rel))) fail('STAGE_INSIDE_SOURCE');
  // Enumerate and verify before making any output. Only manifest members enter.
  const sourceFiles = pins.files.map(item => ({ ...item, file: regularFile(sourceReal, item.path) }));
  fs.mkdirSync(destReal);
  for (const item of sourceFiles) {
    const bytes = fs.readFileSync(item.file);
    if (bytes.length !== item.bytes || sha256(bytes) !== item.sha256) fail('SOURCE_CHANGED_DURING_STAGE');
    const target = path.join(destReal, ...item.path.split('/'));
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, bytes, { flag: 'wx', mode: item.mode ?? 0o644 });
  }
  inspectSource(destReal, pins);
  fs.writeFileSync(path.join(destReal, '.maimi-native-candidate.json'), JSON.stringify({ ...checked, source_archive_sha256: TARGET.sourceArchiveSha256, company_publish_allowed: false }) + '\n', { flag: 'wx' });
  return { ...checked, status: 'STAGED', copied_files: sourceFiles.length, overwritten_files: 0, upstream_ui_overlays: 0 };
}
