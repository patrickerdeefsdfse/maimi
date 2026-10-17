// Self-contained for serialization into the reviewed enterprise UI plugin.
// The shared Office service retains its authenticated JSON heartbeat contract.
export function installOfficeResponseBridge(target = globalThis) {
  const previous = target.fetch;
  if (typeof previous !== 'function') throw new TypeError('Office preview fetch unavailable');
  let active = true;
  const maxPdf = 32 * 1024 * 1024, maxResponse = 48 * 1024 * 1024;
  const invalid = () => { throw new TypeError('文档预览返回异常，可以先下载原文件。'); };
  const fetch = async function (input, init) {
    if (!active) return previous.call(this, input, init);
    let url;
    try { url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url, target.document?.baseURI || target.location?.href); }
    catch { return previous.call(this, input, init); }
    const method = String(init?.method ?? input?.method ?? 'GET').toUpperCase();
    if (url.origin !== target.location?.origin || url.pathname !== '/api/officeToPdf/render'
        || url.search || method !== 'POST') return previous.call(this, input, init);
    const signal = init?.signal ?? input?.signal;
    signal?.throwIfAborted();
    const response = await previous.call(this, input, init);
    if (!response.ok || response.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase() !== 'application/json') return response;
    const declared = response.headers.get('content-length');
    if (declared && /^\d+$/.test(declared) && Number(declared) > maxResponse) {
      await response.body?.cancel(); invalid();
    }
    const reader = response.body?.getReader();
    if (!reader) invalid();
    const abortRead = () => { reader.cancel(signal.reason).catch(() => {}); };
    signal?.addEventListener('abort', abortRead, { once: true });
    const chunks = []; let length = 0;
    try {
      for (;;) {
        signal?.throwIfAborted();
        const { done, value } = await reader.read();
        if (done) break;
        length += value.byteLength;
        if (length > maxResponse) invalid();
        chunks.push(value);
      }
    } catch (error) { await reader.cancel().catch(() => {}); throw error; }
    finally { signal?.removeEventListener('abort', abortRead); reader.releaseLock(); }
    signal?.throwIfAborted();
    const bytes = new Uint8Array(length); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    chunks.length = 0;
    let envelope;
    try { envelope = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
    catch { invalid(); }
    const headers = new Headers(response.headers);
    for (const name of ['content-length', 'content-encoding', 'transfer-encoding']) headers.delete(name);
    // Keep the exact failure body and error vocabulary; the native JSON path
    // handles it. A failed multipart envelope is not accepted by upstream.
    if (envelope?.result?.ok !== true) return new Response(bytes, { status: response.status, statusText: response.statusText, headers });
    const value = envelope.result.value;
    if (envelope.type !== 'server-response' || typeof envelope.rpcId !== 'string' || !envelope.rpcId || envelope.rpcId.length > 256
        || envelope.attachments !== undefined || !value || typeof value !== 'object' || Array.isArray(value)
        || typeof value.absolutePath !== 'string' || !value.absolutePath || value.absolutePath.length > 4096
        || typeof value.version !== 'string' || !value.version || value.version.length > 4096
        || !Number.isSafeInteger(value.bytes) || value.bytes < 5 || value.bytes > maxPdf
        || value.offset !== 0 || value.eof !== true || typeof value.data !== 'string'
        || value.data.length !== Math.ceil(value.bytes / 3) * 4
        || /[^A-Za-z0-9+/=]/.test(value.data)
        || !Array.isArray(value.missingFonts) || value.missingFonts.length > 1024
        || value.missingFonts.some(item => typeof item !== 'string' || item.length > 512)
        || typeof value.generation !== 'string' || !value.generation || value.generation.length > 128) invalid();
    let decoded;
    try { decoded = atob(value.data); } catch { invalid(); }
    if (decoded.length !== value.bytes || !decoded.startsWith('%PDF-') || btoa(decoded) !== value.data) invalid();
    const pdf = new Uint8Array(decoded.length);
    for (let index = 0; index < decoded.length; index++) pdf[index] = decoded.charCodeAt(index);
    decoded = undefined;
    signal?.throwIfAborted();
    const form = new FormData();
    form.set('metadata', JSON.stringify({ ...envelope, result: { ...envelope.result, value: { ...value, data: null } },
      attachments: [{ path: ['data'], part: 'dsh-preview-bytes', codec: 'bytes' }] }));
    form.set('dsh-preview-bytes', new Blob([pdf], { type: 'application/pdf' }), 'preview.pdf');
    headers.delete('content-type');
    return new Response(form, { status: response.status, statusText: response.statusText, headers });
  };
  target.fetch = fetch;
  return () => { active = false; if (target.fetch === fetch) target.fetch = previous; };
}
