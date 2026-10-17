'use strict';
// Private key remains outside this repository and every server/client package.
const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const keyDir = path.join(process.env.LOCALAPPDATA, 'MEGMEET', 'release-signing');
const keyFile = path.join(keyDir, 'desktop-ed25519.pem');
const policyFile = path.join(root, 'desktop-local/update-policy.json');
const sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
if (process.argv[2] === 'init') {
  fs.mkdirSync(keyDir, { recursive: true });
  if (!fs.existsSync(keyFile)) {
    const pair = crypto.generateKeyPairSync('ed25519');
    fs.writeFileSync(keyFile, pair.privateKey.export({ type: 'pkcs8', format: 'pem' }), { flag: 'wx', mode: 0o600 });
  }
  const privateKey = crypto.createPrivateKey(fs.readFileSync(keyFile));
  const publicKey = crypto.createPublicKey(privateKey).export({ type: 'spki', format: 'pem' }).toString();
  const policy = { keyId: sha(publicKey).slice(0, 24), publicKey };
  if (fs.existsSync(policyFile) && JSON.stringify(JSON.parse(fs.readFileSync(policyFile))) !== JSON.stringify(policy)) throw Error('Existing trust key differs; explicit rotation required');
  fs.writeFileSync(policyFile, JSON.stringify(policy, null, 2) + '\n');
  console.log('PUBLIC_RELEASE_KEY: ' + policy.keyId);
} else if (process.argv[2] === 'publish') {
  const installer = path.resolve(process.argv[3]), site = path.resolve(process.argv[4]);
  const version = JSON.parse(fs.readFileSync(path.join(root, 'desktop-local/package.json'))).version;
  const file = `MEGMEET-AI-Setup-${version}-x64.exe`;
  if (path.basename(installer) !== file) throw Error('Installer filename differs');
  const body = fs.readFileSync(installer), hash = sha(body), bytes = body.length;
  const privateKey = crypto.createPrivateKey(fs.readFileSync(keyFile)), policy = JSON.parse(fs.readFileSync(policyFile));
  const issued = Date.now(), sequence = Math.floor(issued / 1000);
  const payload = JSON.stringify({ product: 'com.megmeet.ai', channel: 'company', version, sequence, platform: 'win32', arch: 'x64',
    issuedAt: new Date(issued).toISOString(), expiresAt: new Date(issued + 365 * 86400000).toISOString(), artifact: { file, bytes, sha256: hash } });
  const envelope = { schema: 1, keyId: policy.keyId, payload, signature: crypto.sign(null, Buffer.from(payload), privateKey).toString('base64') };
  require('../../desktop-local/updater.cjs').verifyManifest(JSON.stringify(envelope), policy);
  fs.mkdirSync(path.join(site, 'releases'), { recursive: true }); fs.mkdirSync(path.join(site, 'updates'), { recursive: true });
  const target = path.join(site, 'releases', file);
  if (fs.existsSync(target) && sha(fs.readFileSync(target)) !== hash) throw Error('Published version is immutable');
  fs.copyFileSync(installer, target);
  fs.writeFileSync(path.join(site, 'updates/company-win-x64.json'), JSON.stringify(envelope) + '\n');
  fs.writeFileSync(path.join(site, 'release.json'), JSON.stringify({ schema_version: 1, status: 'published', version, filename: file, size_bytes: bytes, sha256: hash }, null, 2) + '\n');
  console.log('RELEASE_SIGNED_AND_STAGED: ' + version); console.log('INSTALLER_SHA256: ' + hash);
} else throw Error('Use init or publish <installer> <site directory>');
