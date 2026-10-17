// Local rehearsal only. Deny every outbound TCP connection except loopback;
// never installed into the company runtime or used to bypass TLS.
import net from 'node:net';
import { syncBuiltinESMExports } from 'node:module';
const original = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  const first = Array.isArray(args[0]) ? args[0][0] : args[0];
  const host = first && typeof first === 'object' ? first.host : typeof args[1] === 'string' ? args[1] : 'localhost';
  if (host && !['127.0.0.1', '::1', 'localhost'].includes(host)) throw Error('COMMUNITY_PROBE_EXTERNAL_NETWORK_DENIED');
  return original.apply(this, args);
};
syncBuiltinESMExports();
