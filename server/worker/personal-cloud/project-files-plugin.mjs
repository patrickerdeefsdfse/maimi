import { createHash } from 'node:crypto';
import {Readable} from 'node:stream';
import {FileTransferGate,releaseWithResponse} from './project-file-transfer-gate.mjs';

// Private native-host seam. Both the cloud wrapper and gateway MUST deny the
// /api/megmeet-cloud/ prefix to clients. This is not a browser upload endpoint.
export const inject = ['sessions', 'sessionPersistence', 'attachments', 'connection'];
export const LIMITS = Object.freeze({ bytes: 32 * 1024 * 1024, cardPrintBytes: 128 * 1024 * 1024, events: 50_000,
  eventBytes: 64 * 1024 * 1024, references: 1024, requestBytes: 45 * 1024 * 1024 });
const SID = /^(?:session-)?[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
// DSH AttachmentId is the branded digest, not the on-disk hash basename.
const HASH = /^sha256:[a-f0-9]{64}$/;
const MEDIA = new Set(['image/png', 'image/jpeg', 'image/webp', 'image/gif']);
const fail = (code, status = 400) => { throw Object.assign(Error(code), { code, status }); };
const plain = value => value && typeof value === 'object' && !Array.isArray(value);
function name(value) {
  if (typeof value !== 'string' || !value || value.length > 240 || /[\\/\x00-\x1f\x7f]/.test(value) || value === '.' || value === '..')
    fail('project_filename_invalid');
  return value;
}
function reference(value, kind, maximum = LIMITS.bytes) {
  if (!plain(value) || !HASH.test(value.attachmentId || '') || !Number.isSafeInteger(value.bytes)
      || value.bytes < 0 || value.bytes > maximum) fail('project_attachment_invalid');
  const result = { attachmentId: value.attachmentId, name: name(value.name || (kind === 'image' ? 'image' : '')), bytes: value.bytes };
  if (kind === 'image') {
    if (!MEDIA.has(value.mediaType) || !Number.isSafeInteger(value.width) || value.width < 1
        || !Number.isSafeInteger(value.height) || value.height < 1) fail('project_attachment_invalid');
    Object.assign(result, { mediaType: value.mediaType, width: value.width, height: value.height });
    if (value.originalDimensions) result.originalDimensions = value.originalDimensions;
  } else if (kind !== 'file') fail('project_attachment_kind_invalid');
  return result;
}
function bytes(value, maximum) {
  if (typeof value !== 'string' || value.length % 4)
    fail('project_file_encoding_invalid');
  const padding = value.endsWith('==') ? 2 : value.endsWith('=') ? 1 : 0;
  if (value.length > Math.ceil(maximum / 3) * 4 || value.length / 4 * 3 - padding > maximum)
    fail('project_attachment_too_large', 413);
  // A repeated-group regexp can overflow V8's regexp stack on a valid 32 MiB
  // file. Decode once within the byte bound, then require exact canonical form.
  const data = Buffer.from(value, 'base64');
  if (data.length > maximum || data.toString('base64') !== value) fail('project_file_encoding_invalid');
  return data;
}
function schema(body, fields) {
  if (!plain(body) || Object.keys(body).some(key => !fields.includes(key))) fail('project_request_invalid');
}
function projection(events, output, budget, signal) {
  for (const event of events) {
    signal?.throwIfAborted();
    budget.events++;
    budget.bytes += Buffer.byteLength(JSON.stringify(event));
    if (budget.events > LIMITS.events || budget.bytes > LIMITS.eventBytes) fail('project_session_too_large_reattach', 413);
    // Only actual accepted user messages grant access. Tool/assistant JSON and
    // staged receipts are never interpreted as attachment authorization.
    if (event?.type !== 'user/message' || event.data?.source?.kind !== 'user' || !Array.isArray(event.data.content)) continue;
    for (const part of event.data.content) {
      if (!['file', 'image'].includes(part?.type)) continue;
      const ref = reference(part.attachment, part.type);
      const key = `${part.type}:${ref.attachmentId}:${ref.name}`;
      output.set(key, { kind: part.type, ref });
      if (output.size > LIMITS.references) fail('project_session_too_many_attachments', 413);
    }
  }
}
export async function listReferences(ctx, sessionId, signal) {
  if (!SID.test(sessionId || '')) fail('project_session_invalid');
  const output = new Map(), budget = { events: 0, bytes: 0 };
  const attached = ctx.sessions.get(sessionId);
  if (attached) {
    // snapshotEvents is a valid append-only prefix; later appends cannot change
    // references accepted in that prefix. No chat text leaves this function.
    const events = attached.snapshotEvents();
    if (events.length > LIMITS.events) fail('project_session_too_large_reattach', 413);
    projection(events, output, budget, signal);
  } else {
    const before = await ctx.sessionPersistence.stat(sessionId, { signal });
    if (!before) fail('project_session_not_found', 404);
    if ((before.eventCount || 0) > LIMITS.events || (before.sizeBytes || 0) > LIMITS.eventBytes)
      fail('project_session_too_large_reattach', 413);
    const handle = await ctx.sessionPersistence.open(sessionId, 'read', { signal });
    try {
      if (handle.id !== sessionId) fail('project_session_mismatch', 409);
      let offset = 0;
      for (;;) {
        signal?.throwIfAborted();
        const result = await handle.read(offset, Math.min(256, LIMITS.events + 1 - offset), { signal });
        if (!Array.isArray(result.events)) fail('project_session_invalid', 502);
        projection(result.events, output, budget, signal);
        offset += result.events.length;
        if (!result.events.length || result.events.length < 256) break;
      }
      const after = await ctx.sessionPersistence.stat(sessionId, { signal });
      if (!after || after.revision !== before.revision) fail('project_session_changed_retry', 409);
    } finally { await handle.close(); }
  }
  signal?.throwIfAborted();
  return [...output.values()];
}
// Routing input comes from the last *accepted native step*, never from staged
// uploads, the latest historical filename, model text, or a browser-supplied ID.
export function currentStep(ctx, sessionId, signal) {
  if (!SID.test(sessionId || '')) fail('project_session_invalid');
  const attached = ctx.sessions.get(sessionId);
  if (!attached) fail('project_active_session_required', 409);
  const events = attached.snapshotEvents();
  if (events.length > LIMITS.events) fail('project_session_too_large_reattach', 413);
  let messages = [], budget = 0, step = null;
  for (const event of events) {
    signal?.throwIfAborted();
    budget += Buffer.byteLength(JSON.stringify(event));
    if (budget > LIMITS.eventBytes) fail('project_session_too_large_reattach', 413);
    if (event?.type === 'step/start') { messages = []; step = event.data?.step; }
    if (event?.type !== 'user/message' || event.data?.source?.kind !== 'user' || step === null) continue;
    const rpcId = event.data.source.rpcId;
    if (typeof rpcId !== 'string' || rpcId.length > 128 || !Array.isArray(event.data.content)) continue;
    const refs = new Map();
    projection([event], refs, {events: 0, bytes: 0}, signal);
    messages.push({request_id: rpcId, items: [...refs.values()]});
    if (messages.length > 32) fail('project_step_too_many_messages', 413);
  }
  return {messages};
}
async function read(ctx, item, maximum, signal, cardPrint = false) {
  // Large card documents use only the streaming binary endpoint. JSON aliases
  // remain bounded to ordinary files so no private caller can revive base64 OOM.
  const ceiling = LIMITS.bytes;
  const ref = reference(item.ref, item.kind, ceiling);
  if (!Number.isSafeInteger(maximum) || maximum < 0 || maximum > ceiling || ref.bytes > maximum)
    fail('project_attachment_too_large', 413);
  let data;
  if (item.kind === 'image') {
    const result = await ctx.attachments.readImage(ref, signal);
    data = Buffer.from(result.data);
  } else {
    const parts = []; let size = 0;
    for await (const chunk of ctx.attachments.readFileStream(ref, signal)) {
      signal?.throwIfAborted(); size += chunk.byteLength;
      if (size > maximum || size > ref.bytes) fail('project_attachment_length_mismatch', 502);
      parts.push(Buffer.from(chunk));
    }
    data = Buffer.concat(parts, size);
  }
  signal?.throwIfAborted();
  if (data.length !== ref.bytes || 'sha256:' + createHash('sha256').update(data).digest('hex') !== ref.attachmentId)
    fail('project_attachment_integrity_failed', 502);
  return { kind: item.kind, ref, data_base64: data.toString('base64') };
}
export async function dispatch(ctx, body, signal) {
  signal?.throwIfAborted();
  if (body?.operation === 'current-step') {
    schema(body, ['operation', 'session_id']);
    return currentStep(ctx, body.session_id, signal);
  }
  if (body?.operation === 'list') {
    schema(body, ['operation', 'session_id']);
    return { items: await listReferences(ctx, body.session_id, signal) };
  }
  if (body?.operation === 'read') {
    schema(body, ['operation', 'session_id', 'ref', 'kind', 'maximum']);
    const expected = reference(body.ref, body.kind);
    const found = (await listReferences(ctx, body.session_id, signal)).find(item => item.kind === body.kind
      && item.ref.attachmentId === expected.attachmentId && item.ref.name === expected.name
      && item.ref.bytes === expected.bytes);
    if (!found) fail('project_attachment_not_in_session', 403);
    return read(ctx, found, body.maximum, signal);
  }
  if (['read-generated','read-card-print'].includes(body?.operation)) {
    // The authenticated gateway may use only a committed owner/session registry
    // record here. This route is never a model tool or client-supplied hash read.
    schema(body, ['operation', 'session_id', 'ref', 'kind', 'maximum']);
    if (!SID.test(body.session_id || '')) fail('project_session_invalid');
    await listReferences(ctx, body.session_id, signal);
    const cardPrint = body.operation === 'read-card-print';
    if (cardPrint && (body.kind !== 'file' || body.ref?.name !== '员工工卡.docx')) fail('project_card_print_invalid');
    return read(ctx, { kind: body.kind, ref: body.ref }, body.maximum, signal, cardPrint);
  }
  if (['save','save-card-print'].includes(body?.operation)) {
    schema(body, ['operation', 'session_id', 'name', 'data_base64']);
    // Prove the native session still exists before committing. Gateway and worker
    // separately check the live publishing permit before and after this call.
    await listReferences(ctx, body.session_id, signal);
    const cardPrint = body.operation === 'save-card-print';
    const ceiling = LIMITS.bytes;
    const filename = name(body.name);
    if (cardPrint && filename !== '员工工卡.docx') fail('project_card_print_invalid');
    const data = bytes(body.data_base64, ceiling);
    const hash = 'sha256:' + createHash('sha256').update(data).digest('hex');
    async function* chunks() { for (let i = 0; i < data.length; i += 65536) { signal?.throwIfAborted(); yield data.subarray(i, i + 65536); } }
    const ref = reference(await ctx.attachments.saveFileStream({ name: filename, data: chunks(), signal }), 'file', ceiling);
    signal?.throwIfAborted();
    if (ref.attachmentId !== hash || ref.bytes !== data.length) fail('project_save_integrity_failed', 502);
    return { kind: 'file', ref };
  }
  fail('project_operation_not_found', 404);
}
async function boundedJson(request) {
  const claimed = request.headers.get('content-length');
  if (claimed && (!/^\d+$/.test(claimed) || Number(claimed) > LIMITS.requestBytes)) fail('project_request_too_large', 413);
  if (!request.body) fail('project_request_invalid');
  const reader = request.body.getReader(), chunks = []; let total = 0;
  try {
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      total += value.byteLength;
      if (total > LIMITS.requestBytes) fail('project_request_too_large', 413);
      chunks.push(Buffer.from(value));
    }
    return JSON.parse(Buffer.concat(chunks, total).toString('utf8'));
  } catch (error) { await reader.cancel().catch(() => {}); if (error.code) throw error; fail('project_request_invalid'); }
  finally { reader.releaseLock(); }
}
function cardMetadata(request) {
  const value = request.headers.get('x-megmeet-card-metadata');
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value) || value.length > 16384) fail('project_card_print_invalid');
  try {
    const bytes = Buffer.from(value, 'base64url');
    if (bytes.toString('base64url') !== value) fail('project_card_print_invalid');
    const metadata = JSON.parse(bytes.toString('utf8'));
    if (!plain(metadata)) fail('project_card_print_invalid');
    return metadata;
  } catch { fail('project_card_print_invalid'); }
}
export async function cardPrintResponse(ctx, request) {
  const body = cardMetadata(request), signal = request.signal;
  if (!SID.test(body.session_id || '')) fail('project_session_invalid');
  await listReferences(ctx, body.session_id, signal);
  if (body.operation === 'save-card-print') {
    schema(body, ['operation','session_id','name','bytes','sha256']);
    if (body.name !== '员工工卡.docx' || !Number.isSafeInteger(body.bytes) || body.bytes < 1
        || body.bytes > LIMITS.cardPrintBytes || !/^[a-f0-9]{64}$/.test(body.sha256 || '')) fail('project_card_print_invalid');
    const claimed = request.headers.get('content-length');
    if (claimed !== null && claimed !== String(body.bytes)) fail('project_attachment_length_mismatch', 400);
    if (!request.body) fail('project_request_invalid');
    async function* chunks() {
      const reader = request.body.getReader(), hash = createHash('sha256'); let size = 0;
      try {
        for (;;) {
          signal.throwIfAborted();
          const {done,value} = await reader.read(); if (done) break;
          size += value.byteLength;
          if (size > body.bytes || size > LIMITS.cardPrintBytes) fail('project_attachment_too_large', 413);
          hash.update(value); yield value;
        }
        signal.throwIfAborted();
        // EOF validation occurs before saveFileStream can commit its temporary object.
        if (size !== body.bytes) fail('project_attachment_length_mismatch', 400);
        if (hash.digest('hex') !== body.sha256) fail('project_attachment_integrity_failed', 400);
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
    }
    const ref = reference(await ctx.attachments.saveFileStream({name:body.name,data:chunks(),signal}), 'file', LIMITS.cardPrintBytes);
    signal.throwIfAborted();
    if (ref.bytes !== body.bytes || ref.attachmentId !== 'sha256:' + body.sha256) fail('project_save_integrity_failed', 502);
    return Response.json({kind:'file',ref}, {headers:{'Cache-Control':'no-store'}});
  }
  if (body.operation === 'read-card-print') {
    schema(body, ['operation','session_id','kind','ref','maximum']);
    if (body.kind !== 'file' || body.ref?.name !== '员工工卡.docx') fail('project_card_print_invalid');
    const ref = reference(body.ref, 'file', LIMITS.cardPrintBytes);
    if (!Number.isSafeInteger(body.maximum) || body.maximum < ref.bytes || body.maximum > LIMITS.cardPrintBytes)
      fail('project_attachment_too_large', 413);
    if (request.headers.get('content-length') && request.headers.get('content-length') !== '0') fail('project_request_invalid');
    async function* chunks() {
      const hash = createHash('sha256'); let size = 0;
      for await (const value of ctx.attachments.readFileStream(ref, signal)) {
        signal.throwIfAborted(); size += value.byteLength;
        if (size > ref.bytes) fail('project_attachment_length_mismatch', 502);
        hash.update(value); yield value;
      }
      signal.throwIfAborted();
      if (size !== ref.bytes || 'sha256:' + hash.digest('hex') !== ref.attachmentId) fail('project_attachment_integrity_failed', 502);
    }
    return new Response(Readable.toWeb(Readable.from(chunks())), {headers:{'Content-Type':'application/octet-stream',
      'Content-Length':String(ref.bytes),'Cache-Control':'no-store'}});
  }
  fail('project_operation_not_found', 404);
}
export function apply(ctx) {
  const transfers=new FileTransferGate();
  ctx.effect(() => ctx.connection.fetch.register({
    path:'/api/megmeet-cloud/card-print', methods:['POST'], requestBody:'streaming',
    fetch:async request => {
      let release;
      try {
        release=await transfers.acquire(request.signal);
        const response=releaseWithResponse(await cardPrintResponse(ctx,request),release);
        release=null;return response;
      } catch(error) {return Response.json({detail:typeof error.code==='string'&&error.code.startsWith('project_')?error.code:'project_file_operation_failed'},
        {status:Number.isInteger(error.status)?error.status:502,headers:{'Cache-Control':'no-store'}});}
      finally {release?.();}
    }
  }), 'private streaming print attachment');
  ctx.effect(() => ctx.connection.fetch.register({
    path: '/api/megmeet-cloud/project-files', methods: ['POST'], requestBody: 'streaming',
    fetch: async request => {
      let release;
      try {
        release=await transfers.acquire(request.signal);
        const result=await dispatch(ctx,await boundedJson(request),request.signal);
        const response=releaseWithResponse(Response.json(result,{headers:{'Cache-Control':'no-store'}}),release);
        release=null;return response;
      }
      catch (error) { return Response.json({ detail: typeof error.code === 'string' && error.code.startsWith('project_') ? error.code : 'project_file_operation_failed' },
        { status: Number.isInteger(error.status) ? error.status : 502, headers: { 'Cache-Control': 'no-store' } }); }
      finally { release?.(); }
    }
  }), 'private project attachment projection');
}
