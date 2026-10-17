'use strict';
// Offline verification and staging only. Activation belongs to a future
// signed installer; this code never executes archives or changes user data.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { assertSupportedSet, sha } = require('./index.cjs');
function check(value, code) { if (!value) throw Object.assign(Error(code), { code }); }
function fileSha(file) {
  const hash = crypto.createHash('sha256'), fd = fs.openSync(file, 'r');
  const buffer = Buffer.alloc(1024 * 1024);
  try { let count; while ((count = fs.readSync(fd, buffer, 0, buffer.length, null)) > 0) hash.update(buffer.subarray(0, count)); }
  finally { fs.closeSync(fd); }
  return hash.digest('hex');
}
function verifyUpdate({ envelope, policy, artifact, now = Date.now() }) {
  check(policy && policy.schema === 1 && policy.channel === 'company'
    && Number.isSafeInteger(policy.minimumSequence) && policy.minimumSequence >= 0
    && policy.publicKeys && Object.keys(policy.publicKeys).length > 0, 'UPDATE_TRUST_POLICY_REQUIRED');
  check(envelope && envelope.schema === 1 && typeof envelope.keyId === 'string'
    && Object.hasOwn(policy.publicKeys, envelope.keyId)
    && typeof envelope.payload === 'string' && Buffer.byteLength(envelope.payload) <= 256 * 1024
    && typeof envelope.signature === 'string' && /^[A-Za-z0-9+/]{86}==$/.test(envelope.signature), 'UPDATE_SIGNATURE_REQUIRED');
  const key = crypto.createPublicKey(policy.publicKeys[envelope.keyId]);
  check(key.asymmetricKeyType === 'ed25519', 'UPDATE_KEY_ALGORITHM_UNSUPPORTED');
  check(crypto.verify(null, Buffer.from(envelope.payload, 'utf8'), key, Buffer.from(envelope.signature, 'base64')), 'UPDATE_SIGNATURE_INVALID');
  const release = JSON.parse(envelope.payload);
  check(release.schema === 1 && release.channel === policy.channel
    && Number.isSafeInteger(release.sequence) && release.sequence > policy.minimumSequence, 'UPDATE_ROLLBACK_OR_CHANNEL_DENIED');
  const created = Date.parse(release.issuedAt), expires = Date.parse(release.expiresAt);
  check(Number.isFinite(created) && Number.isFinite(expires) && created <= now + 300000
    && expires > now && expires > created && expires - created <= 30 * 86400000, 'UPDATE_EXPIRED_OR_CLOCK_INVALID');
  check(typeof release.desktopVersion === 'string' && /^\d+\.\d+\.\d+$/.test(release.desktopVersion), 'UPDATE_VERSION_INVALID');
  assertSupportedSet(release.releaseSet);
  check(release.releaseSet.adapterSha256 === sha(fs.readFileSync(path.join(__dirname, 'index.cjs'))), 'UPDATE_ADAPTER_REQUIRES_DESKTOP_RELEASE');
  const spec = release.artifact;
  check(spec && typeof spec.name === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.zip$/.test(spec.name)
    && Number.isSafeInteger(spec.bytes) && spec.bytes > 0 && spec.bytes <= 8 * 1024 ** 3
    && /^[0-9a-f]{64}$/.test(spec.sha256), 'UPDATE_ARTIFACT_INVALID');
  const info = fs.lstatSync(artifact);
  check(info.isFile() && !info.isSymbolicLink() && path.basename(artifact) === spec.name
    && info.size === spec.bytes && fileSha(artifact) === spec.sha256, 'UPDATE_ARTIFACT_INTEGRITY');
  return { release, status: 'verified-not-activated' };
}
function stageUpdate({ envelope, policy, artifact, directory, now }) {
  const result = verifyUpdate({ envelope, policy, artifact, now });
  fs.mkdirSync(directory, { recursive: true });
  check(!fs.lstatSync(directory).isSymbolicLink(), 'UPDATE_STAGE_SYMLINK_DENIED');
  // Exclusive immutable staging directory. An old receipt or candidate is never
  // overwritten, and verification is repeated on copied bytes.
  const target = path.join(directory, 'candidate-' + result.release.sequence);
  fs.mkdirSync(target);
  const copy = path.join(target, result.release.artifact.name);
  fs.copyFileSync(artifact, copy, fs.constants.COPYFILE_EXCL);
  verifyUpdate({ envelope, policy, artifact: copy, now });
  fs.writeFileSync(path.join(target, 'signed-manifest.json'), JSON.stringify(envelope, null, 2) + '\n', { flag: 'wx' });
  fs.writeFileSync(path.join(target, 'STAGED-NOT-INSTALLED.json'), JSON.stringify({ schema: 1,
    sequence: result.release.sequence, artifactSha256: result.release.artifact.sha256,
    requires: ['signed-installer', 'history-recovery-acceptance', 'running-session-shutdown'],
  }, null, 2) + '\n', { flag: 'wx' });
  return { ...result, directory: target };
}
module.exports = { verifyUpdate, stageUpdate, fileSha };
