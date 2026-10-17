'use strict';
// Whole-desktop releases are authenticated independently from IAM/TokenKey.
// No renderer-controlled URL, credential, path or command reaches this module.
const fs = require('node:fs');
const path = require('node:path');
const https = require('node:https');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const ORIGIN = 'https://platform-ai.megmeet.com';
const FEED = ORIGIN + '/maimi/updates/company-win-x64.json';
const MAX_FEED = 16384;
const MAX_INSTALLER = 2 * 1024 ** 3;
const DAY = 86400000;
const INSTALL_GRACE = 5 * 60000;
const INSTALL_LIVENESS_LIMIT = 60 * 60000;
const DEFAULT_TIMEOUTS = Object.freeze({ connection: 15000, checking: 45000,
  downloadIdle: 45000, downloadTotal: 30 * 60000, verifying: 120000 });
const INSTALLER_NAME = /^MEGMEET-AI-Setup-(\d+\.\d+\.\d+)-x64\.exe$/;
const PARTIAL_NAME = /^MEGMEET-AI-Setup-\d+\.\d+\.\d+-x64\.exe\.[a-f0-9]{16}\.partial$/;
function check(value, message) { if (!value) throw Error(message); }
function versionParts(value) {
  check(typeof value === 'string' && /^(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})$/.test(value), 'Invalid release version');
  return value.split('.').map(Number);
}
function compareVersions(a, b) {
  const aa = versionParts(a), bb = versionParts(b);
  for (let i = 0; i < 3; i++) if (aa[i] !== bb[i]) return Math.sign(aa[i] - bb[i]);
  return 0;
}
function verifyManifest(raw, policy, now = Date.now()) {
  check(Buffer.byteLength(raw) <= MAX_FEED, 'Update feed too large');
  const envelope = JSON.parse(raw);
  check(envelope?.schema === 1 && envelope.keyId === policy.keyId && typeof envelope.payload === 'string', 'Unknown update signing key');
  check(typeof envelope.signature === 'string' && /^[A-Za-z0-9+/]{86}==$/.test(envelope.signature), 'Invalid release signature');
  const publicKey = crypto.createPublicKey(policy.publicKey);
  check(publicKey.asymmetricKeyType === 'ed25519', 'Invalid public key algorithm');
  check(crypto.verify(null, Buffer.from(envelope.payload, 'utf8'), publicKey, Buffer.from(envelope.signature, 'base64')), 'Release signature rejected');
  const item = JSON.parse(envelope.payload);
  check(item.product === 'com.megmeet.ai' && item.channel === 'company' && item.platform === 'win32' && item.arch === 'x64', 'Wrong release target');
  versionParts(item.version);
  check(Number.isSafeInteger(item.sequence) && item.sequence > 0, 'Invalid release sequence');
  const issued = Date.parse(item.issuedAt), expires = Date.parse(item.expiresAt);
  check(Number.isFinite(issued) && Number.isFinite(expires) && issued <= now + 300000 && expires > now && expires > issued && expires - issued <= 366 * DAY, 'Release expired or clock incorrect');
  const a = item.artifact;
  check(a && a.file === `MEGMEET-AI-Setup-${item.version}-x64.exe`, 'Invalid installer name');
  check(Number.isSafeInteger(a.bytes) && a.bytes > 0 && a.bytes <= MAX_INSTALLER && /^[a-f0-9]{64}$/.test(a.sha256), 'Invalid installer integrity metadata');
  return item;
}
function request(url, onResponse, signal, onConnected = () => {}) {
  const u = new URL(url);
  check(u.origin === ORIGIN && !u.username && !u.password && !u.search && !u.hash &&
    (u.href === FEED || /^\/maimi\/releases\/MEGMEET-AI-Setup-\d+\.\d+\.\d+-x64\.exe$/.test(u.pathname)), 'Update URL denied');
  return new Promise((resolve, reject) => {
    let req, response, handling = false, settled = false, failure;
    const finish = (error, value) => {
      if (settled) return;
      settled = true; signal?.removeEventListener('abort', aborted);
      if (error || failure) reject(error && error.code !== 'ABORT_ERR' ? error : failure || error); else resolve(value);
    };
    const fail = error => {
      if (settled || failure) return;
      failure = error;
      // Once the response handler owns a file, wait for its finally/close before
      // releasing the operation slot. A timeout must not start a second writer.
      response?.destroy(error); req?.destroy(error);
      if (!handling) finish(failure);
    };
    const aborted = () => fail(signal.reason || Error('Update cancelled'));
    if (signal?.aborted) { aborted(); return; }
    signal?.addEventListener('abort', aborted, { once: true });
    try {
      req = https.get(u, { signal, headers: { 'User-Agent': 'MEGMEET-Desktop-Updater/1', 'Accept-Encoding': 'identity' } }, res => {
        if (settled) { res.destroy(); return; }
        response = res;
        res.on('error', fail);
        // No redirects, cookies, browser session, custom CA bypass or IAM tokens.
        if (res.statusCode !== 200 || (res.headers['content-encoding'] && res.headers['content-encoding'] !== 'identity')) {
          res.destroy(); finish(Error('Update server unavailable')); return;
        }
        handling = true;
        Promise.resolve().then(() => { signal?.throwIfAborted(); onConnected(); return onResponse(res); })
          .then(value => finish(undefined, value), error => { res.destroy(); finish(error); });
      });
      // The updater's connection/phase/progress deadlines are shorter and also
      // cover DNS and pre-socket waits. Retain a final transport idle bound.
      req.setTimeout(60000, () => fail(Error('Update request timed out')));
      req.on('error', fail);
    } catch (error) { fail(error); }
  });
}
async function fetchFeed(signal, { onConnected } = {}) {
  return request(FEED, async res => {
    const chunks = []; let length = 0;
    for await (const part of res) { length += part.length; check(length <= MAX_FEED, 'Update feed too large'); chunks.push(part); }
    return Buffer.concat(chunks).toString('utf8');
  }, signal, onConnected);
}
async function hashFile(file, signal) {
  signal?.throwIfAborted();
  const hash = crypto.createHash('sha256'); let bytes = 0;
  for await (const part of fs.createReadStream(file, { signal })) { bytes += part.length; check(bytes <= MAX_INSTALLER, 'Installer too large'); hash.update(part); }
  return { bytes, sha256: hash.digest('hex') };
}
async function downloadInstaller(item, file, signal, onProgress = () => {}, { onConnected } = {}) {
  const partial = file + '.' + crypto.randomBytes(8).toString('hex') + '.partial';
  try {
    await request(ORIGIN + '/maimi/releases/' + item.artifact.file, async res => {
      const length = res.headers['content-length'];
      if (length !== undefined) check(Number(length) === item.artifact.bytes, 'Installer length differs');
      const handle = await fs.promises.open(partial, 'wx', 0o600);
      const hash = crypto.createHash('sha256'); let received = 0;
      let lastProgress = 0;
      const progress = force => {
        const at = Date.now();
        if (force || at - lastProgress >= 250) { lastProgress = at; onProgress({ received, total: item.artifact.bytes, percent: Math.floor(received * 100 / item.artifact.bytes) }); }
      };
      progress(true);
      try {
        for await (const part of res) {
          signal?.throwIfAborted();
          received += part.length; check(received <= item.artifact.bytes, 'Installer exceeds declared size');
          hash.update(part);
          let offset = 0;
          while (offset < part.length) { const { bytesWritten } = await handle.write(part, offset, part.length - offset); check(bytesWritten > 0, 'Installer write failed'); offset += bytesWritten; }
          progress(received === item.artifact.bytes);
        }
        check(received === item.artifact.bytes && hash.digest('hex') === item.artifact.sha256, 'Installer integrity rejected');
        signal?.throwIfAborted();
        await handle.sync();
      } finally { await handle.close(); }
    }, signal, onConnected);
    signal?.throwIfAborted();
    await fs.promises.rename(partial, file);
  } finally { await fs.promises.unlink(partial).catch(() => {}); }
}
function atomicJson(file, data) {
  const partial = file + '.tmp';
  fs.writeFileSync(partial, JSON.stringify(data) + '\n', { mode: 0o600 });
  fs.renameSync(partial, file);
}
function readJson(file) {
  try { check(fs.statSync(file).size <= MAX_FEED * 2, 'Local updater state too large'); return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch { return undefined; }
}
function removeOwnedFile(file) {
  try {
    const stat = fs.lstatSync(file);
    if (stat.isFile() && !stat.isSymbolicLink() && stat.nlink === 1) fs.unlinkSync(file);
  } catch { /* Locked or already absent: leave it for a later check. */ }
}
function pruneCache(directory, keep = new Set(), removePartials = false) {
  // Only names emitted by this updater, one directory deep. Never recurse into
  // anything, follow a link, or delete an arbitrary EXE/workspace/data file.
  for (const name of fs.readdirSync(directory)) {
    if (keep.has(name)) continue;
    const match = INSTALLER_NAME.exec(name);
    if (match) {
      try { versionParts(match[1]); } catch { continue; }
      removeOwnedFile(path.join(directory, name));
    } else if (removePartials && PARTIAL_NAME.test(name)) removeOwnedFile(path.join(directory, name));
  }
}
function inspectInstallerProcess(pid) {
  if (process.platform !== 'win32' || !Number.isSafeInteger(pid) || pid <= 0 || pid > 0xffffffff) return undefined;
  // Query only the recorded child PID's executable path and creation time.
  // Never enumerate other processes or read any process command line.
  const script = `[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; try { $p=Get-Process -Id ${pid}; [pscustomobject]@{file=$p.Path;startedAt=$p.StartTime.ToUniversalTime().ToString('o')} | ConvertTo-Json -Compress } catch { exit 1 }`;
  try {
    const result = spawnSync(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe'),
      ['-NoProfile', '-NonInteractive', '-Command', script], { windowsHide: true, shell: false, encoding: 'utf8', timeout: 5000, maxBuffer: 4096 });
    if (result.status !== 0) return undefined;
    const value = JSON.parse(result.stdout);
    return typeof value.file === 'string' ? { file: value.file, startedAt: Date.parse(value.startedAt) } : undefined;
  } catch { return undefined; }
}
function failureCode(error, phase = 'checking') {
  const code = error?.code;
  if (['check-timeout', 'connection-timeout', 'download-stalled', 'download-timeout', 'verification-timeout'].includes(code)) return code;
  if (code === 'ENOSPC') return 'storage-full';
  if (['EACCES', 'EPERM', 'EROFS'].includes(code)) return 'storage-permission';
  if (['CERT_HAS_EXPIRED', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'SELF_SIGNED_CERT_IN_CHAIN', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'ERR_TLS_CERT_ALTNAME_INVALID'].includes(code)) return 'tls-verification';
  const message = error?.message;
  if (['Release signature rejected', 'Unknown update signing key', 'Invalid release signature', 'Invalid public key algorithm'].includes(message)) return 'signature-rejected';
  if (message === 'Release expired or clock incorrect') return 'clock-or-expired';
  if (['Release rollback rejected', 'Release sequence reused'].includes(message)) return 'rollback-rejected';
  if (['Installer length differs', 'Installer exceeds declared size', 'Installer integrity rejected', 'Cached installer integrity rejected', 'Invalid cached installer'].includes(message)) return 'integrity-rejected';
  if (message === 'Portable build: install the setup release first') return 'portable-install-required';
  if (message === 'Update request timed out' || ['ETIMEDOUT', 'ECONNABORTED'].includes(code)) return 'network-timeout';
  if (phase === 'installing') return 'installer-launch-failed';
  return phase === 'checking' ? 'check-unavailable' : 'download-unavailable';
}
function createUpdater({ currentVersion, directory, policy, onStatus = () => {},
  getFeed = fetchFeed, download = downloadInstaller, launch = launchInstaller, now = Date.now,
  inspectProcess = inspectInstallerProcess, timeouts = DEFAULT_TIMEOUTS,
  schedule = setTimeout, unschedule = clearTimeout }) {
  versionParts(currentVersion);
  directory = path.resolve(directory);
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  check(!fs.lstatSync(directory).isSymbolicLink(), 'Updater directory must not be a link');
  const receiptFile = path.join(directory, 'ready.json'), sequenceFile = path.join(directory, 'sequence.json');
  const attemptFile = path.join(directory, 'install-attempt.json');
  const limits = { ...DEFAULT_TIMEOUTS, ...timeouts };
  for (const value of Object.values(limits)) check(Number.isSafeInteger(value) && value > 0 && value <= 3600000, 'Invalid update timeout');
  let stopped = false, started = false, failedChecks = 0, nextCheckAt, installationResult, inFlight, ready, timer, abort, status = 'idle';
  let retryTimer, availableVersion, progress, checkedAt, errorCode, lastDetail;
  let busy = false, cleanupPending = false, operationStartedAt, lastProgressAt;
  function snapshot() {
    return Object.freeze({ status, currentVersion, availableVersion: ready?.item.version || availableVersion,
      ...(progress ? { progress: { ...progress } } : {}), checkedAt, errorCode,
      busy, cleanupPending, operationStartedAt, lastProgressAt,
      canInstall: !stopped && !busy && !!ready && ['ready', 'install-failed'].includes(status),
      automaticInstall: 'user-requested', automaticCheck: true, automaticDownload: true, nextCheckAt, installationResult });
  }
  function emit(value, detail) {
    status = value; lastDetail = detail;
    try { onStatus(value, ready?.item.version || availableVersion, detail, snapshot()); } catch {}
  }
  async function bounded(phase, work, { allowStopped = false } = {}) {
    if (stopped && !allowStopped) throw Error('Update stopped');
    const controller = new AbortController(); abort = controller;
    let connectionTimer, idleTimer, connected = false;
    const expire = code => {
      if (controller.signal.aborted) return;
      const error = Object.assign(Error(code), { code });
      if (!stopped) { errorCode = code; cleanupPending = true; emit('unavailable'); }
      controller.abort(error);
    };
    const total = phase === 'checking' ? limits.checking : phase === 'verifying' ? limits.verifying : limits.downloadTotal;
    const totalCode = phase === 'checking' ? 'check-timeout' : phase === 'verifying' ? 'verification-timeout' : 'download-timeout';
    const totalTimer = setTimeout(() => expire(totalCode), total);
    if (phase !== 'verifying') connectionTimer = setTimeout(() => expire('connection-timeout'), limits.connection);
    const touch = () => {
      if (phase !== 'downloading' || controller.signal.aborted) return;
      clearTimeout(idleTimer); idleTimer = setTimeout(() => expire('download-stalled'), limits.downloadIdle);
    };
    const onConnected = () => {
      if (connected || controller.signal.aborted) return;
      connected = true; clearTimeout(connectionTimer);
      if (phase === 'downloading') { touch(); emit('downloading'); }
    };
    try {
      const result = await work(controller.signal, { onConnected, touch });
      controller.signal.throwIfAborted();
      return result;
    } catch (error) {
      throw controller.signal.aborted ? controller.signal.reason : error;
    } finally {
      clearTimeout(totalTimer); clearTimeout(connectionTimer); clearTimeout(idleTimer);
      if (abort === controller) abort = undefined;
      cleanupPending = false;
    }
  }
  function signedRecord(file) {
    const record = readJson(file);
    try { return { ...record, item: verifyManifest(record.manifest, policy, now()) }; }
    catch { return undefined; }
  }
  function previousAttempt() {
    const record = signedRecord(attemptFile);
    if (!record) return undefined;
    if (compareVersions(record.item.version, currentVersion) <= 0) {
      installationResult = { status: 'confirmed', expectedVersion: record.item.version,
        actualVersion: currentVersion, confirmedAt: now() };
      // A process handoff is not success: only the newly running application's
      // real package version can acknowledge the completed installation.
      atomicJson(path.join(directory, 'installed.json'), installationResult);
      removeOwnedFile(attemptFile); return undefined;
    }
    if (!Number.isSafeInteger(record.attemptedAt) || record.attemptedAt > now() + 300000) return undefined;
    return record;
  }
  function cleanCache({ removePartials = false, candidate } = {}) {
    const keep = new Set();
    for (const record of [signedRecord(receiptFile), previousAttempt(), ready, candidate]) {
      if (record?.item && compareVersions(record.item.version, currentVersion) > 0) keep.add(record.item.artifact.file);
    }
    pruneCache(directory, keep, removePartials);
  }
  function isInstalling(attempt) {
    if (!attempt || attempt.state !== 'handed-off') return false;
    const age = now() - attempt.attemptedAt;
    if (age < INSTALL_GRACE) return true;
    if (age >= INSTALL_LIVENESS_LIMIT || !Number.isSafeInteger(attempt.pid) || attempt.pid <= 0) return false;
    let live;
    try { live = inspectProcess(attempt.pid); } catch { return false; }
    return Boolean(live && typeof live.file === 'string' && path.resolve(live.file).toLowerCase() === path.join(directory, attempt.item.artifact.file).toLowerCase() &&
      Number.isFinite(live.startedAt) && live.startedAt >= attempt.attemptedAt - 10000 && live.startedAt <= attempt.attemptedAt + 60000);
  }
  function reportReady() {
    const attempt = previousAttempt();
    emit(attempt?.item.version === ready?.item.version ? 'install-failed' : 'ready',
      attempt ? 'previous-install-incomplete' : undefined);
  }
  // The main process holds the per-user single-instance lock before creating
  // the updater. No download belonging to this instance has started yet.
  cleanCache({ removePartials: true });
  function validateReceipt(receipt) {
    check(receipt && typeof receipt.manifest === 'string', 'Missing release receipt');
    const item = verifyManifest(receipt.manifest, policy, now());
    check(compareVersions(item.version, currentVersion) > 0, 'No newer release');
    validateSequence(item, receipt.manifest);
    return { item, manifest: receipt.manifest, file: path.join(directory, item.artifact.file) };
  }
  function validateSequence(item, manifest) {
    const highest = readJson(sequenceFile);
    check(!highest || (Number.isSafeInteger(highest.sequence) && typeof highest.digest === 'string'), 'Invalid release sequence');
    check(!highest || item.sequence >= highest.sequence, 'Release rollback rejected');
    check(!highest || item.sequence !== highest.sequence || crypto.createHash('sha256').update(manifest).digest('hex') === highest.digest, 'Release sequence reused');
  }
  async function validFile(candidate, signal) {
    signal?.throwIfAborted();
    const stat = fs.lstatSync(candidate.file);
    check(stat.isFile() && !stat.isSymbolicLink() && stat.size === candidate.item.artifact.bytes, 'Invalid cached installer');
    const integrity = await hashFile(candidate.file, signal);
    check(integrity.sha256 === candidate.item.artifact.sha256, 'Cached installer integrity rejected');
  }
  async function performCheck() {
    if (stopped) return;
    const attempt = previousAttempt();
    if (isInstalling(attempt)) {
      emit('installing', 'previous-install-pending');
      clearTimeout(retryTimer);
      retryTimer = setTimeout(() => { void checkNow(); }, Math.max(30000, INSTALL_GRACE - (now() - attempt.attemptedAt)));
      retryTimer.unref?.(); return;
    }
    progress = undefined; lastProgressAt = undefined; errorCode = undefined; emit('checking');
    try {
      // A fresh feed takes priority over an already downloaded release. Failed
      // connectivity keeps a previously verified, unexpired signed receipt.
      const manifest = await bounded('checking', (signal, hooks) => getFeed(signal, hooks));
      const item = verifyManifest(manifest, policy, now());
      // Even a signed current/older feed must pass the last accepted sequence.
      // A stale feed must never turn a failed newer download into "up to date".
      validateSequence(item, manifest);
      availableVersion = item.version;
      atomicJson(sequenceFile, { sequence: item.sequence, digest: crypto.createHash('sha256').update(manifest).digest('hex') });
      if (compareVersions(item.version, currentVersion) <= 0) { ready = undefined; cleanCache(); emit('current'); return; }
      const candidate = validateReceipt({ manifest });
      ready = undefined;
      cleanCache({ candidate });
      let cached = false;
      if (fs.existsSync(candidate.file)) {
        emit('verifying');
        try { await bounded('verifying', signal => validFile(candidate, signal)); cached = true; }
        catch (error) { if (stopped || error?.code === 'verification-timeout') throw error; }
      }
      if (!cached) {
        progress = { received: 0, total: item.artifact.bytes, percent: 0 };
        emit('connecting');
        await bounded('downloading', async (signal, hooks) => {
          let connectedAt;
          const onConnected = () => { connectedAt ??= now(); hooks.onConnected(); };
          await download(item, candidate.file, signal, value => {
            if (stopped || signal.aborted || !Number.isSafeInteger(value?.received) || !Number.isSafeInteger(value?.total) || value.total !== item.artifact.bytes || value.received < (progress?.received || 0) || value.received > value.total) return;
            onConnected();
            const increased = value.received > (progress?.received || 0);
            const at = now();
            if (increased) { lastProgressAt = at; hooks.touch(); }
            progress = { received: value.received, total: value.total, percent: Math.floor(value.received * 100 / value.total) };
            const elapsed = (at - connectedAt) / 1000;
            if (value.received > 0 && elapsed > 0) {
              const bytesPerSecond = Math.round(value.received / elapsed);
              if (bytesPerSecond > 0) Object.assign(progress, { bytesPerSecond, remainingSeconds: Math.ceil((value.total - value.received) / bytesPerSecond) });
            }
            emit('downloading');
          }, { onConnected });
        });
        emit('verifying'); await bounded('verifying', signal => validFile(candidate, signal));
      }
      if (stopped) return;
      atomicJson(receiptFile, { manifest }); ready = candidate; cleanCache(); reportReady();
    } catch (error) {
      if (stopped) return;
      errorCode = failureCode(error, status === 'checking' ? 'checking' : 'downloading');
      try { const candidate = validateReceipt(readJson(receiptFile)); await bounded('verifying', signal => validFile(candidate, signal)); ready = candidate; reportReady(); }
      catch { ready = undefined; emit(attempt ? 'install-failed' : 'unavailable', attempt ? 'previous-install-incomplete' : undefined); }
    } finally { abort = undefined; checkedAt = now(); }
  }
  function checkNow() {
    if (stopped) return Promise.resolve();
    if (!inFlight) {
      busy = true; operationStartedAt = now(); cleanupPending = false;
      inFlight = performCheck().finally(() => {
        inFlight = undefined; busy = false; cleanupPending = false;
        if (!stopped) {
          if (started) {
            const failed = status === 'unavailable' || !!errorCode;
            failedChecks = failed ? Math.min(failedChecks + 1, 5) : 0;
            const interval = failed ? Math.min(5 * 60000 * 2 ** (failedChecks - 1), 3600000) : 2 * 3600000;
            unschedule(timer); nextCheckAt = now() + interval;
            timer = schedule(() => { timer = undefined; void checkNow(); }, interval); timer.unref?.();
          }
          emit(status, lastDetail);
        }
      });
    }
    return inFlight;
  }
  function start() { if (started || stopped) return; started = true; void checkNow(); }
  function resume() { if (started && !stopped && (!checkedAt || now() - checkedAt >= 5 * 60000)) void checkNow(); }
  function stop() { stopped = true; unschedule(timer); nextCheckAt = undefined; clearTimeout(retryTimer); abort?.abort(); }
  // This method is called only after Electron's normal quit cleanup. It checks
  // signed metadata and actual executable bytes again; no shell command text.
  async function installAfterQuit({ systemShutdown = false, applicationPath } = {}) {
    stop();
    if (systemShutdown || !ready || isInstalling(previousAttempt())) return false;
    try {
      const candidate = validateReceipt({ manifest: ready.manifest });
      await bounded('verifying', signal => validFile(candidate, signal), { allowStopped: true });
      const attempt = { manifest: candidate.manifest, attemptedAt: now(), state: 'handed-off' };
      atomicJson(attemptFile, attempt);
      const child = await launch(candidate.file, applicationPath);
      if (Number.isSafeInteger(child?.pid) && child.pid > 0) {
        // A failed receipt update must not turn a successful process handoff
        // into an immediate second attempt. The original grace record remains.
        try { atomicJson(attemptFile, { ...attempt, pid: child.pid }); } catch {}
      }
      return true;
    } catch (error) {
      errorCode = failureCode(error, 'installing');
      try { atomicJson(attemptFile, { manifest: ready.manifest, attemptedAt: now(), state: 'launch-failed' }); } catch {}
      emit('install-failed', 'installer-launch-failed'); return false;
    }
  }
  return { start, stop, resume, checkNow, installAfterQuit, get status() { return status; }, get snapshot() { return snapshot(); } };
}
function launchInstaller(file, applicationPath) {
  check(process.platform === 'win32' && typeof applicationPath === 'string' && path.isAbsolute(applicationPath), 'Installer requires an installed Windows application');
  check(!/[\x00-\x1f"]/.test(applicationPath), 'Invalid installation path');
  // Only an installation made by our per-user NSIS package may auto-update.
  // A portable archive must not silently create a second installation.
  check(fs.existsSync(path.join(applicationPath, 'Uninstall MEGMEET-AI.exe')), 'Portable build: install the setup release first');
  return new Promise((resolve, reject) => {
    // NSIS /D must be last and unquoted; it consumes the remaining command line
    // including spaces. No shell is involved, and the path is owned by main.
    const child = spawn(file, ['--updated', '/currentuser', '/D=' + applicationPath], {
      detached: true, windowsHide: false, windowsVerbatimArguments: true, stdio: 'ignore', shell: false });
    child.once('error', reject);
    child.once('spawn', () => { child.unref(); resolve({ pid: child.pid }); });
  });
}
module.exports = { createUpdater, verifyManifest, compareVersions, downloadInstaller, fetchFeed, launchInstaller, failureCode, ORIGIN, FEED };
