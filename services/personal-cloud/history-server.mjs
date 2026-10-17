/** Private, short-lived offline maintenance worker. Never starts DSH. */
import http from 'node:http';
import { timingSafeEqual } from 'node:crypto';
import { createHistoryMaintenance } from './history-native.mjs';

const token = process.env.CLOUD_ROUTE_TOKEN || '';
if (!/^[A-Za-z0-9_-]{43}$/.test(token) || process.env.CLOUD_MAINTENANCE !== 'exclusive-offline-v1') throw Error('maintenance configuration');
let busy = false;
const server = http.createServer(async (req, res) => {
  const provided = Buffer.from(req.headers.authorization || ''), expected = Buffer.from('Bearer ' + token);
  const reply = (status, value) => { res.writeHead(status, {'Content-Type':'application/json','Cache-Control':'no-store'}); res.end(JSON.stringify(value)); };
  if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) return reply(401, {error:'UNAUTHORIZED'});
  if (req.url === '/__cloud/ready' && req.method === 'GET') return reply(200, {mode:'exclusive-offline-v1'});
  if (req.url !== '/__cloud/history' || req.method !== 'POST') return reply(404, {error:'NOT_FOUND'});
  if (busy) return reply(409, {error:'MAINTENANCE_BUSY'});
  busy = true;
  try {
    let raw = '';
    for await (const part of req) { raw += part; if (Buffer.byteLength(raw) > 4096) throw Error('REQUEST_LIMIT'); }
    const body = JSON.parse(raw);
    const adapter = createHistoryMaintenance({stateRoot:'/state', runtimeRoot:'/opt/dsh',
      assertQuiescent: async () => ({stopped:true, exclusive:true}),
      confirmArchived: async ({sessionId,digest}) => ({stored:body.sessionId === sessionId && body.archivedDigest === digest, digest:body.archivedDigest}),
      maxSessions:2000, maxBytes:4*1024*1024, maxDecodedBytes:16*1024*1024});
    const actions = {
      inventory: () => adapter.inventory(),
      'delete-session': () => adapter.deleteSession({sessionId:body.sessionId}),
      'archived-sessions': () => adapter.archivedSessions(),
    };
    if (!Object.hasOwn(actions,body.action)) throw Error('INVALID_ACTION');
    const result = await actions[body.action]();
    if (Buffer.byteLength(JSON.stringify(result)) > 32*1024*1024) throw Error('RESPONSE_LIMIT');
    reply(200, {result});
  } catch (error) {
    const code = /^[A-Z_]{1,80}$/.test(error.code || '') ? error.code : 'MAINTENANCE_REFUSED';
    reply(409, {error:code});
  } finally { busy = false; }
});
server.requestTimeout=60000;
server.listen(3084,'0.0.0.0');
setTimeout(() => { server.close(); process.exit(0); },120000).unref();
