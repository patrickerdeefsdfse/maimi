'use strict';
// Main-process, OS-encrypted device credentials. Never a password, IAM token,
// model key or desktop short access token; no plaintext fallback is permitted.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const MAXIMUM = 65536;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
const TOKEN = /^mdr_[A-Za-z0-9_-]{43}$/;
const PROOF = /^[A-Za-z0-9_-]{43}$/;
const fail = code => { throw Object.assign(Error(code), {code}); };
const keys = (value, names) => value && typeof value === 'object' && !Array.isArray(value)
  && Object.keys(value).every(key => names.includes(key));

function validateRecord(value) {
  if (!keys(value, ['deviceId', 'instanceId', 'verifier', 'token', 'enrollmentId', 'expiresAt', 'user', 'pending'])
      || !UUID.test(value.deviceId) || !UUID.test(value.instanceId) || !PROOF.test(value.verifier)
      || !TOKEN.test(value.token) || !UUID.test(value.enrollmentId)
      || !Number.isSafeInteger(value.expiresAt) || value.expiresAt < 0) fail('DEVICE_STORAGE_INVALID');
  if (value.user !== null && (!keys(value.user, ['user_no', 'account_no']) || typeof value.user.user_no !== 'string'
      || !value.user.user_no || value.user.user_no.length > 200
      || (value.user.account_no !== undefined && (typeof value.user.account_no !== 'string' || value.user.account_no.length > 200))))
    fail('DEVICE_STORAGE_INVALID');
  if (value.pending !== null && (!keys(value.pending, ['requestId', 'nextToken'])
      || !UUID.test(value.pending.requestId) || !TOKEN.test(value.pending.nextToken))) fail('DEVICE_STORAGE_INVALID');
  return value;
}

function validateDocument(value) {
  if (!keys(value, ['schema', 'active', 'revocations']) || value.schema !== 1
      || !Array.isArray(value.revocations) || value.revocations.length > 16) fail('DEVICE_STORAGE_INVALID');
  if (value.active !== null) validateRecord(value.active);
  value.revocations.forEach(validateRecord);
  return value;
}

function createRememberedStore({directory, safeStorage, platform = process.platform, io = fs}) {
  const file = path.join(directory, 'remembered-device-v1.bin');
  // A non-secret sign-out marker prevents reuse even if decryption is temporarily unavailable.
  const blockedFile = path.join(directory, 'remembered-device-v1.signed-out');
  const available = () => safeStorage?.isEncryptionAvailable?.() === true
    && (platform !== 'linux' || !['basic_text', 'unknown'].includes(safeStorage.getSelectedStorageBackend?.() || 'unknown'));
  function regular(target) {
    if (!io.existsSync(target)) return false;
    const stat = io.lstatSync(target);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > MAXIMUM) fail('DEVICE_STORAGE_INVALID');
    return true;
  }
  function writeBytes(target, bytes) {
    io.mkdirSync(directory, {recursive: true});
    const dir = io.lstatSync(directory);
    if (!dir.isDirectory() || dir.isSymbolicLink()) fail('DEVICE_STORAGE_INVALID');
    regular(target);
    const temp = target + '.' + crypto.randomUUID() + '.tmp';
    let handle;
    try {
      handle = io.openSync(temp, 'wx', 0o600);
      io.writeFileSync(handle, bytes); io.fsyncSync(handle); io.closeSync(handle); handle = undefined;
      io.renameSync(temp, target);
    } finally {
      if (handle !== undefined) io.closeSync(handle);
      if (io.existsSync(temp)) io.unlinkSync(temp);
    }
  }
  return {
    available, blocked: () => regular(blockedFile),
    block() { writeBytes(blockedFile, Buffer.from('{"schema":1}\n')); },
    unblock() { if (regular(blockedFile)) io.unlinkSync(blockedFile); },
    read() {
      if (!regular(file)) return {schema: 1, active: null, revocations: []};
      if (!available()) fail('DEVICE_ENCRYPTION_UNAVAILABLE');
      let plain;
      try { plain = safeStorage.decryptString(io.readFileSync(file)); }
      catch { fail('DEVICE_DECRYPTION_UNAVAILABLE'); }
      if (typeof plain !== 'string' || Buffer.byteLength(plain) > MAXIMUM) fail('DEVICE_STORAGE_INVALID');
      let value; try { value = JSON.parse(plain); } catch { fail('DEVICE_STORAGE_INVALID'); }
      return validateDocument(value);
    },
    write(value) {
      validateDocument(value);
      if (!available()) fail('DEVICE_ENCRYPTION_UNAVAILABLE');
      const encrypted = safeStorage.encryptString(JSON.stringify(value));
      if (!Buffer.isBuffer(encrypted) || !encrypted.length || encrypted.length > MAXIMUM) fail('DEVICE_STORAGE_INVALID');
      writeBytes(file, encrypted);
    },
  };
}

module.exports = {createRememberedStore, validateDocument, validateRecord};
