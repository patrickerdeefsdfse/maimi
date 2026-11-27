import http from 'node:http';
import {Readable} from 'node:stream';

// The local long-running project endpoint sends its headers with the final result.
// Its caller supplies both the tool deadline and the original cancellation signal.
export function requestProjectTool(url, options) {
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.pathname !== '/__megmeet/tool-execute' ||
      url.username || url.password || url.search || url.hash || options.method !== 'POST' || !options.signal) {
    throw Error('TOOLKIT_PRIVATE_ENDPOINT_REQUIRED');
  }
  options.signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const req = http.request(url, {method: 'POST', headers: options.headers, agent: false, signal: options.signal}, reply => {
      const body = Readable.toWeb(reply);
      resolve(new Response(body, {status: reply.statusCode, headers: {'Content-Type': 'application/json'}}));
    });
    req.once('error', reject);
    req.end(options.body);
  });
}
