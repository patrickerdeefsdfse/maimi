// Product composition around the unmodified, pinned official provider.
// The credential is a short-lived private bridge ticket, never a TokenKey key.
const TICKET = /^mst_[A-Za-z0-9_-]{43}$/;
const stop = (code) => { throw Object.assign(Error(code), { code }); };

async function json(response, maximum = 128 * 1024) {
  let size = 0; const chunks = [];
  for await (const chunk of response.body || []) {
    size += chunk.byteLength; if (size > maximum) { await response.body?.cancel().catch(() => {}); stop('WEB_RESPONSE_TOO_LARGE'); }
    chunks.push(Buffer.from(chunk));
  }
  try { return JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { stop('WEB_RESPONSE_INVALID'); }
}

export function createOfficialFirstSearch({ DeepSeekSearchProvider, endpoint, acquireScope, releaseScope,
  recordRequest, recordOutcome = () => {}, fetchImpl = fetch }) {
  const base = new URL(endpoint);
  if (base.protocol !== 'http:' || base.hostname !== '127.0.0.1' || !base.port
      || base.pathname !== '/__megmeet/search' || base.search || base.hash || base.username || base.password)
    stop('SEARCH_PRIVATE_ENDPOINT_REQUIRED');
  return { id: 'megmeet-gateway', available: () => true,
    async search(request, callerSignal) {
      if (typeof request?.query !== 'string' || !request.query.trim() || request.query.length > 400
          || (request.maxResults !== undefined && (!Number.isInteger(request.maxResults) || request.maxResults < 1 || request.maxResults > 10)))
        stop('WEB_ARGUMENTS_INVALID');
      const signal = AbortSignal.any([callerSignal, AbortSignal.timeout(85000)].filter(Boolean));
      signal.throwIfAborted();
      const ticket = await acquireScope();
      if (!TICKET.test(ticket || '')) stop('SEARCH_NATIVE_SCOPE_REQUIRED');
      const call = async (operation, body) => {
        signal.throwIfAborted();
        const response = await fetchImpl(base.href + '/' + operation, { method: 'POST', redirect: 'error',
          headers: { Authorization: 'Bearer ' + ticket, 'Content-Type': 'application/json' },
          body: JSON.stringify(body), signal });
        const value = await json(response); signal.throwIfAborted();
        if (!response.ok) stop(value.code || 'WEB_UNAVAILABLE');
        return value;
      };
      try {
        const official = new DeepSeekSearchProvider(() => ({ apiKey: ticket, baseURL: base.href,
          model: 'deepseek-flash-v4.1', apiVersion: '2023-06-01', maxTokens: 2048, maxUses: 1,
          recordRequest: value => recordRequest?.(value) }));
        let result;
        try {
          result = await official.search(request, signal);
          signal.throwIfAborted();
          // Scope revalidation is required even if the provider parsed a 200.
          await call('decision', {});
          if (!Array.isArray(result.sources) || result.sources.length === 0) throw Error('SEARCH_EMPTY_SOURCES');
          recordOutcome({ provider: 'deepseek-official', fallback: false });
          return { ...result, content: '搜索服务：DeepSeek 官方搜索。', sources: result.sources.slice(0, request.maxResults ?? 5),
            truncated: result.truncated || result.sources.length > (request.maxResults ?? 5) };
        } catch (error) {
          signal.throwIfAborted();
          // No parsing of arbitrary upstream error text and no credential retry.
          // The private bridge, not the model, authoritatively allows fallback.
          const decision = await call('decision', {});
          if (decision.fallback_allowed !== true) stop(decision.code || 'WEB_UNAVAILABLE');
          const result = await call('fallback', { query: request.query, count: request.maxResults ?? 5 });
          recordOutcome({ provider: 'bing-public', fallback: true, reason: decision.code });
          if (!Array.isArray(result.sources) || !result.sources.length) stop('SEARCH_BING_FAILED');
          return { ...result, content: '搜索服务：Bing（备用检索；官方搜索当前不可用）。' };
        }
      } finally { await releaseScope(ticket); }
    } };
}
