import {captureToolScope,acquireToolTicket,releaseToolTicket} from './native-task-context.mjs';
// The native web seam uses the authenticated employee's managed capability
// without requiring a skill selection. No provider key or external URL is a transport.
export const name = 'megmeet-cloud-web-control';
export const inject = ['agents', 'web'];
export const PROVIDER = 'megmeet-gateway';
const SESSION = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const fail = code => { throw Object.assign(Error(code), {code}); };

// Runtime ownership, not durable fork metadata, determines which conversation
// granted the tool. A resumed fork is an independent root and gains no grant.
export function initiatingRoot(agents) {
  let agent = agents.currentInitiator();
  const live = agents.list(), roots = new Set(agents.roots()), seen = new Set();
  if (live.length > 512) fail('WEB_SESSION_REQUIRED');
  while (agent && !seen.has(agent) && live.includes(agent)) {
    seen.add(agent);
    if (roots.has(agent)) {
      if (!SESSION.test(agent.session?.id || '') || agent.id !== agent.session.id) break;
      return agent.session.id;
    }
    const owners = live.filter(owner => agents.isOwnedBy(agent.id, owner));
    if (owners.length !== 1) break;
    agent = owners[0];
  }
  fail('WEB_SESSION_REQUIRED');
}

export function apply(ctx, config) {
  const endpoint = new URL(config.endpoint);
  if (endpoint.href !== 'http://127.0.0.1:3086/__megmeet/web'
      || !/^[A-Za-z0-9_-]{43}$/.test(config.token || '')) fail('WEB_PRIVATE_ENDPOINT_REQUIRED');
  const call = async (action, args, signal) => {
    const scope=captureToolScope(ctx.agents.currentInitiator()),sessionId=scope.rootSessionId;
    const ticket=await acquireToolTicket(scope,'web-read');
    try{
    const response = await fetch(endpoint, {method: 'POST', redirect: 'error',
      headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + ticket},
      body: JSON.stringify({action, arguments: args, session_id: sessionId}),
      signal: AbortSignal.any([signal, AbortSignal.timeout(90000)].filter(Boolean))});
    const text = await response.text();
    if (text.length > 128 * 1024) fail('WEB_RESPONSE_TOO_LARGE');
    let result; try { result = JSON.parse(text); } catch { fail('WEB_RESPONSE_INVALID'); }
    if (!response.ok) throw Object.assign(Error(result.message || '联网功能暂时不可用，请稍后重试。'),
      {code: result.code || 'WEB_UNAVAILABLE'});
    return result;
    }finally{await releaseToolTicket(ticket);}
  };
  if(!config.officialSearch)ctx.web.registerSearchProvider({id: PROVIDER, available: () => true,
    search: (request, signal) => call('web_search', {query: request.query,
      count: Math.min(10, Math.max(1, Number.isSafeInteger(request.maxResults) ? request.maxResults : 5))}, signal)});
  ctx.web.registerFetchProvider({id: PROVIDER, available: () => true,
    fetch: (request, signal) => call('web_read', {url: request.url}, signal)});
}
