'use strict';
const crypto = require('node:crypto');
const clone = value => JSON.parse(JSON.stringify(value));
const cancelled = () => Object.assign(Error('DEVICE_OPERATION_CANCELLED'), {name: 'AbortError'});
const denied = error => [401, 403].includes(error?.status);

function createDeviceSession({store, api, now = Date.now,
  uuid = () => crypto.randomUUID(), token = () => 'mdr_' + crypto.randomBytes(32).toString('base64url')}) {
  let epoch = 0, pending, abort, stopped = false;
  function snapshot() { return store.read(); }
  function active() {
    if (store.blocked()) return null;
    return snapshot().active;
  }
  function status() {
    try { const doc = snapshot(), blocked = store.blocked();
      return {remembered: !blocked && !!doc.active, storageAvailable: store.available(),
        pendingRevocation: doc.revocations.length > 0 || blocked && !!doc.active}; }
    catch { return {remembered: false, storageAvailable: false, pendingRevocation: store.blocked()}; }
  }
  function check(turn, signal) {
    if (turn !== epoch || stopped || signal.aborted) throw cancelled();
  }
  function run(operation, callerSignal) {
    if (pending) return pending;
    if (stopped) return Promise.reject(cancelled());
    const turn = ++epoch, controller = new AbortController(); abort = controller;
    const signal = AbortSignal.any([callerSignal, controller.signal].filter(Boolean));
    const work = operation(turn, signal).finally(() => {
      if (pending === work) {pending = undefined; abort = undefined;}
    });
    pending = work; return work;
  }
  function cancel() { ++epoch; abort?.abort(); }
  function queueRevocation(record) {
    const doc = snapshot();
    if (!doc.revocations.some(item => item.deviceId === record.deviceId)) doc.revocations.push(clone(record));
    if (doc.active?.deviceId === record.deviceId) doc.active = null;
    store.write(doc);
  }
  async function drainRevocations(signal) {
    let doc;
    try { doc = snapshot(); } catch { return false; }
    // Sign-out marker survives a crash before its encrypted journal update.
    if (store.blocked() && doc.active) {
      queueRevocation(doc.active); doc = snapshot();
    }
    for (const record of doc.revocations) {
      signal?.throwIfAborted();
      try { await api.revokeDevice(record, {signal}); }
      catch (error) { if (!denied(error)) return false; }
      const current = snapshot();
      current.revocations = current.revocations.filter(item => item.deviceId !== record.deviceId);
      store.write(current);
    }
    return true;
  }
  async function restore({signal} = {}) {
    return run(async (turn, operationSignal) => {
      await drainRevocations(operationSignal); check(turn, operationSignal);
      let record = active();
      if (!record) return {status: 'absent'};
      if (record.expiresAt && record.expiresAt <= now()) {
        queueRevocation(record); return {status: 'expired'};
      }
      // Persist the proposed next token and request ID before the server can
      // rotate. Retrying this journal makes a lost response/crash recoverable.
      if (!record.pending) {
        record.pending = {requestId: uuid(), nextToken: token()};
        const doc = snapshot(); doc.active = record; store.write(doc);
      }
      for (let attempt = 0; attempt < 2; attempt++) {
        try {
          const auth = await api.resumeDevice(clone(record), {signal: operationSignal});
          check(turn, operationSignal);
          const doc = snapshot();
          if (store.blocked() || doc.active?.deviceId !== record.deviceId
              || doc.active?.pending?.requestId !== record.pending.requestId) throw cancelled();
          doc.active = {...record, token: record.pending.nextToken, pending: null,
            expiresAt: auth.remembered.expiresAt,
            user: {user_no: auth.user.user_no, ...(auth.user.account_no !== undefined ? {account_no: auth.user.account_no} : {})}};
          store.write(doc);
          return {status: 'restored', auth};
        } catch (error) {
          check(turn, operationSignal);
          if (attempt === 0 && error.status === 409 && error.code === 'desktop_resume_request_stale') {
            // The server confirms the earlier rotation committed but its short
            // replay receipt expired. Only the pre-persisted next token advances.
            record = {...record, token: record.pending.nextToken,
              pending: {requestId: uuid(), nextToken: token()}};
            const doc = snapshot(); doc.active = record; store.write(doc); continue;
          }
          if (denied(error)) {
            const doc = snapshot();
            if (doc.active?.deviceId === record.deviceId) {doc.active = null; store.write(doc);}
            return {status: 'denied'};
          }
          throw error;
        }
      }
      throw Error('DEVICE_RESTORE_INCOMPLETE');
    }, signal);
  }
  async function login(openBrowser, {signal, onStatus} = {}) {
    return run(async (turn, operationSignal) => {
      await drainRevocations(operationSignal); check(turn, operationSignal);
      const supported = store.available() && await api.capabilities({signal: operationSignal});
      check(turn, operationSignal);
      let record;
      if (supported) {
        const doc = snapshot();
        if (doc.active) throw Error('DEVICE_RESTORE_REQUIRED');
        if (doc.revocations.length >= 16) throw Error('DEVICE_SIGNOUT_PENDING');
        record = {deviceId: uuid(), instanceId: uuid(), verifier: crypto.randomBytes(32).toString('base64url'),
          token: token(), enrollmentId: uuid(), expiresAt: 0, user: null, pending: null};
        doc.active = record; store.write(doc);
        // This explicit new login owns a fresh proof, distinct from all queued
        // revocations. Clear the old sign-out marker only after it is durable,
        // so background revocation cannot mistake enrollment for an old login.
        store.unblock();
      }
      try {
        const auth = await api.signIn(openBrowser, {signal: operationSignal, onStatus, device: record && clone(record)});
        check(turn, operationSignal);
        if (record) {
          if (!auth.remembered) {
            queueRevocation(record);
          } else {
            const doc = snapshot();
            if (doc.active?.deviceId !== record.deviceId) throw cancelled();
            doc.active = {...record, expiresAt: auth.remembered.expiresAt,
              user: {user_no: auth.user.user_no, ...(auth.user.account_no !== undefined ? {account_no: auth.user.account_no} : {})}};
            store.write(doc);
          }
        }
        return {auth, remembered: !!auth.remembered, storageAvailable: store.available()};
      } catch (error) {
        if (record) {
          try { queueRevocation(record); } catch {}
          // Cancellation can race an issued exchange. This proof revokes the
          // dedicated device even when no access token reached the application.
          void drainRevocations(AbortSignal.timeout(3000)).catch(() => {});
        }
        throw error;
      }
    }, signal);
  }
  async function forget(auth) {
    // Persist the no-resume decision before canceling any in-flight rotation.
    store.block(); cancel();
    let storageError;
    try { const doc = snapshot(); if (doc.active) queueRevocation(doc.active); }
    catch (error) {storageError = error;}
    const signedOut = await Promise.allSettled([
      drainRevocations(AbortSignal.timeout(10000)), api.signOut(auth, {timeout: 10000}),
    ]);
    if (storageError) throw storageError;
    return signedOut[0].status === 'fulfilled' && signedOut[0].value === true
      && signedOut[1].status === 'fulfilled';
  }
  return {status, restore, login, forget, drainRevocations, cancel,
    stop() {stopped = true; cancel(); return pending?.catch(() => {});},
    get inFlight() {return pending;}};
}

module.exports = {createDeviceSession};
