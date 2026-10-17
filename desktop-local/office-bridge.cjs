'use strict';
// The child DSH process gets only a random loopback lease. The IAM bearer stays
// in this parent process and is attached only to the configured Gateway origin.
const http = require('node:http'), https = require('node:https');
const { timingSafeEqual } = require('node:crypto');
const { Transform, pipeline } = require('node:stream');
const PREFIX = '/__megmeet/office';
const ID = '[A-Za-z0-9_-]{1,128}';
function officeRoute(method, url) {
  if (typeof url !== 'string' || !url.startsWith(PREFIX) || /[%?#]/.test(url)) return null;
  const suffix = url.slice(PREFIX.length);
  if (method === 'GET' && suffix === '/capabilities' || method === 'POST' && suffix === '/jobs') return suffix;
  if (['GET', 'DELETE'].includes(method) && new RegExp('^/jobs/' + ID + '$').test(suffix)) return suffix;
  if (method === 'POST' && new RegExp('^/jobs/' + ID + '/run$').test(suffix)) return suffix;
  if (method === 'PUT' && new RegExp('^/jobs/' + ID + '/inputs/(?:[0-9]|1[0-9])$').test(suffix)) return suffix;
  if (method === 'GET' && new RegExp('^/jobs/' + ID + '/outputs/' + ID + '$').test(suffix)) return suffix;
  return null;
}
function equal(a, b) { const aa = Buffer.from(a || ''), bb = Buffer.from(b); return aa.length === bb.length && timingSafeEqual(aa, bb); }
function bounded(limit) { let size = 0; return new Transform({ transform(chunk, _encoding, callback) {
  size += chunk.length; callback(size > limit ? Error('OFFICE_TRANSFER_TOO_LARGE') : null, chunk);
} }); }
function createOfficeBridge({ gateway, auth, token, alive = () => true, signal }) {
  const origin = new URL(gateway);
  if (origin.pathname !== '/' || origin.search || origin.hash || origin.username || origin.password
      || !(origin.protocol === 'https:' || origin.protocol === 'http:' && origin.hostname === '127.0.0.1')) throw Error('OFFICE_GATEWAY_INVALID');
  if (!/^[A-Za-z0-9_-]{32,128}$/.test(token)) throw Error('OFFICE_LEASE_INVALID');
  const requests = new Set();
  const json = (res, status, detail) => { if (res.headersSent) { res.destroy(); return; }
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify({ detail })); };
  return {
    handle(req, res) {
      if (!equal(req.headers.authorization, 'Bearer ' + token) || !alive() || signal?.aborted || Date.now() >= auth.expiresAt) {
        json(res, 401, 'OFFICE_SIGN_IN_REQUIRED'); req.resume(); return;
      }
      const route = officeRoute(req.method, req.url);
      if (!route) { json(res, 404, 'OFFICE_ROUTE_DENIED'); req.resume(); return; }
      const upload = req.method === 'PUT', download = req.method === 'GET' && route.includes('/outputs/');
      const inputLimit = upload ? 64 * 1024 * 1024 : 1024 * 1024;
      const length = req.headers['content-length'];
      if (length !== undefined && (!/^\d{1,12}$/.test(length) || Number(length) > inputLimit)) {
        json(res, 413, 'OFFICE_TRANSFER_TOO_LARGE'); req.resume(); return;
      }
      const headers = { Authorization: 'Bearer ' + auth.token,
        'Content-Type': upload ? 'application/octet-stream' : 'application/json' };
      if (length !== undefined) headers['Content-Length'] = length;
      const outgoing = (origin.protocol === 'https:' ? https : http).request(new URL('/v1/office' + route, origin),
        { method: req.method, headers }, response => {
          if (response.statusCode >= 300 && response.statusCode < 400) {
            response.resume(); json(res, 502, 'OFFICE_REDIRECT_DENIED'); return;
          }
          // Never forward upstream cookies, redirect targets, or authentication headers.
          res.writeHead(response.statusCode, { 'Content-Type': download && response.statusCode === 200
            ? 'application/octet-stream' : 'application/json', 'Cache-Control': 'no-store' });
          pipeline(response, bounded(download ? 128 * 1024 * 1024 : 2 * 1024 * 1024), res, () => {});
        });
      requests.add(outgoing);
      const abort = () => outgoing.destroy();
      const timer = setTimeout(abort, Math.min(300000, Math.max(1, auth.expiresAt - Date.now())));
      timer.unref(); signal?.addEventListener('abort', abort, { once: true });
      outgoing.on('error', () => json(res, 502, 'OFFICE_SERVICE_UNAVAILABLE'));
      outgoing.once('close', () => { clearTimeout(timer); requests.delete(outgoing); signal?.removeEventListener('abort', abort); });
      req.once('aborted', abort); res.once('close', abort);
      pipeline(req, bounded(inputLimit), outgoing, err => { if (err) json(res, 413, 'OFFICE_TRANSFER_FAILED'); });
    },
    stop() { for (const request of requests) request.destroy(); requests.clear(); },
  };
}
module.exports = { createOfficeBridge, officeRoute };
