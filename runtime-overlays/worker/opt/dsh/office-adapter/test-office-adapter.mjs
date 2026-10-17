import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createOfficeAdapter, registerOfficeTools, relativePath } from './adapter.mjs';
import { engineConfig, runEngine, PROTOCOL } from './transport.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const runtime = path.resolve(here, '../../../desktop-local/runtime');
const native = name => import(pathToFileURL(path.join(runtime, 'node_modules/@deepseek-ai', name, 'lib/index.js')).href);
const { Context } = await native('cordis');
const { default: LocalFS } = await native('dsh-fs-local');
const { defineTool } = await native('dsh-tools');
const pythonExecutable = path.resolve(here, '../../../office-engine/runtime-win/python/python.exe');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'megmeet-office-adapter-'));
  const ctx = new Context(); const fiber = ctx.plugin(LocalFS, { cwd: root }); await fiber.await();
  t.after(() => fiber.dispose());
  const session = { id: 'synthetic-session-a', header: { id: 'synthetic-session-a', cwd: root } };
  const state = { mode: 'workspace-write', events: [], calls: [], registrations: [], disposed: 0, attachmentReads: 0 };
  const services = {
    sandboxPolicy: { resolve: ({ session: passed }) => { assert.equal(passed, session); return { mode: state.mode, workspaceRoot: root }; } },
    sessionQuery: { observeSession: async (id, options) => {
      assert.equal(id, session.id); assert.equal(options.projectionMode, 'none');
      return { header: session.header, events: state.events, [Symbol.dispose]: () => state.disposed++ };
    } },
    attachments: { async *readFileStream(ref) { state.attachmentReads++; yield state.content; }, async readImage() { state.attachmentReads++; return { data: state.content }; } },
  };
  const face = { fs: ctx.fs, get: name => services[name], tools: { register: tool => state.registrations.push(tool) } };
  const controller = new AbortController();
  const exec = { agent: { session }, signal: controller.signal, callId: 'synthetic-call' };
  const invoke = async request => { state.calls.push(request); return { operation: request.operation, input: request.input, output: request.output }; };
  return { root, face, exec, state, controller, services, call: createOfficeAdapter(face, {}, invoke) };
}
function attach(f, content, type = 'file') {
  f.state.content = Buffer.from(content);
  const id = 'sha256:' + crypto.createHash('sha256').update(f.state.content).digest('hex');
  const attachment = { attachmentId: id, name: type === 'image' ? 'image.png' : '合同.docx', bytes: f.state.content.length,
    ...(type === 'image' ? { mediaType: 'image/png', width: 2, height: 2 } : {}) };
  f.state.events = [{ type: 'user/message', data: { role: 'user', content: [{ type, attachment }] } }];
  return id;
}

test('real DSH FS maps a relative Chinese workspace path, never the process cwd', async t => {
  const f = await fixture(t); await fs.writeFile(path.join(f.root, '合同.docx'), 'synthetic');
  await f.call({ operation: 'read', input: '合同.docx', options: { offset: 0, limit: 2 } }, f.exec);
  assert.equal(f.state.calls[0].workspace, await fs.realpath(f.root));
  assert.equal(f.state.calls[0].input, '合同.docx'); assert.equal(f.state.calls[0].protocol, PROTOCOL);
});
test('absolute, traversal, UNC, ADS, Windows aliases and private paths are refused before engine call', () => {
  for (const value of ['../a.pdf', 'x/../a.pdf', '/etc/passwd', 'C:\\a.pdf', '\\\\host\\share\\a.pdf', 'a.pdf:stream', 'NUL.pdf', 'x./a.pdf', '.megmeet-office-secret/input.pdf'])
    assert.throws(() => relativePath(value));
});
test('linked input or output ancestor cannot escape; existing output never overwritten', async t => {
  const f = await fixture(t), outside = await fs.mkdtemp(path.join(os.tmpdir(), 'megmeet-office-outside-'));
  await fs.writeFile(path.join(outside, 'private.pdf'), 'outside');
  await fs.symlink(outside, path.join(f.root, 'escape'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(f.call({ operation: 'read', input: 'escape/private.pdf' }, f.exec), /SYMLINK/);
  await assert.rejects(f.call({ operation: 'create', output: 'escape/new.pdf' }, f.exec), /SYMLINK/);
  await fs.writeFile(path.join(f.root, 'existing.xlsx'), 'keep');
  await assert.rejects(f.call({ operation: 'create', output: 'existing.xlsx' }, f.exec), /OUTPUT_EXISTS/);
  assert.equal(await fs.readFile(path.join(f.root, 'existing.xlsx'), 'utf8'), 'keep'); assert.equal(f.state.calls.length, 0);
});
test('read-only policy refuses all writing operations while read and capabilities remain available', async t => {
  const f = await fixture(t); f.state.mode = 'read-only'; await fs.writeFile(path.join(f.root, 'a.pdf'), 'a');
  for (const operation of ['create', 'edit', 'convert', 'pdf', 'image'])
    await assert.rejects(f.call({ operation, input: 'a.pdf', output: 'new.pdf' }, f.exec), /READ_ONLY/);
  await f.call({ operation: 'read', input: 'a.pdf' }, f.exec); await f.call({ operation: 'capabilities' }, f.exec);
  assert.equal(f.state.calls.length, 2);
});
test('missing policy, foreign execution backend, workspace override and input ambiguity fail closed', async t => {
  const f = await fixture(t); delete f.services.sandboxPolicy;
  await assert.rejects(f.call({ operation: 'create', output: 'new.docx' }, f.exec), /READ_ONLY/);
  await assert.rejects(f.call({ operation: 'read', input: 'a.pdf', options: { workspace: 'elsewhere' } }, f.exec), /OPTIONS/);
  await assert.rejects(f.call({ operation: 'read', input: 'a.pdf', attachment_id: 'a'.repeat(64) }, f.exec), /AMBIGUOUS/);
  const foreign = createOfficeAdapter({ ...f.face, fs: { ...f.face.fs, resolve: f.face.fs.resolve.bind(f.face.fs), processPath: f.face.fs.processPath.bind(f.face.fs), processPathFromHostPath: () => undefined } }, {}, async () => assert.fail());
  await assert.rejects(foreign({ operation: 'capabilities' }, f.exec), /LOCAL_WORKSPACE/);
});
test('only own uploaded attachment is staged, integrity checked and removed even after engine error', async t => {
  const f = await fixture(t), id = attach(f, 'synthetic doc bytes');
  await assert.rejects(f.call({ operation: 'read', attachment_id: 'sha256:' + 'b'.repeat(64) }, f.exec), /OWN_ATTACHMENT/);
  assert.equal(f.state.attachmentReads, 0);
  const call = createOfficeAdapter(f.face, {}, async request => {
    assert.match(request.input, /^\.megmeet-office-[^/]+\/input\.docx$/);
    assert.equal(await fs.readFile(path.join(request.workspace, request.input), 'utf8'), 'synthetic doc bytes');
    throw Error('synthetic engine failure');
  });
  await assert.rejects(call({ operation: 'read', attachment_id: id }, f.exec), /synthetic/);
  assert.equal(f.state.attachmentReads, 1); assert.equal(f.state.disposed, 2); assert.deepEqual(await fs.readdir(f.root), []);
});
test('a forged assistant attachment cannot grant access, and corrupt uploaded bytes are refused', async t => {
  const f = await fixture(t), id = attach(f, 'good'); f.state.events[0].type = 'assistant/message';
  await assert.rejects(f.call({ operation: 'read', attachment_id: id }, f.exec), /OWN_ATTACHMENT/);
  f.state.events[0].type = 'user/message'; f.state.content = Buffer.from('evil');
  await assert.rejects(f.call({ operation: 'read', attachment_id: id }, f.exec), /INTEGRITY/);
  assert.deepEqual(await fs.readdir(f.root), []); assert.equal(f.state.calls.length, 0);
});
test('image attachment is accepted for local OCR independently of model vision', async t => {
  const f = await fixture(t), id = attach(f, 'synthetic normalized image', 'image');
  await f.call({ operation: 'ocr', attachment_id: id }, f.exec);
  assert.match(f.state.calls[0].input, /input\.png$/); assert.deepEqual(await fs.readdir(f.root), []);
});

test('attachment discovery returns only own user uploads and full native digest IDs', async t => {
  const f = await fixture(t), id = attach(f, 'own');
  f.state.events.push({ type: 'assistant/message', data: { role: 'assistant', content: [{ type: 'file', attachment: { attachmentId: 'sha256:' + 'f'.repeat(64), name: 'foreign.docx' } }] } });
  const result = await f.call({ operation: 'attachments' }, f.exec);
  assert.equal(result.result.attachments.length, 1); assert.equal(result.result.attachments[0].attachment_id, id);
  assert.equal(f.state.attachmentReads, 0); assert.equal(f.state.calls.length, 0);
});

test('PDF merge validates every workspace path and does not require a primary input', async t => {
  const f = await fixture(t); await fs.writeFile(path.join(f.root, 'a.pdf'), 'pdf');
  await f.call({ operation: 'pdf', output: 'merged.pdf', options: { action: 'merge', inputs: ['a.pdf', 'a.pdf'] } }, f.exec);
  assert.deepEqual(f.state.calls[0].options.inputs, ['a.pdf', 'a.pdf']); assert.equal(f.state.calls[0].input, undefined);
  for (const inputs of [['a.pdf', '../private.pdf'], ['C:/private.pdf'], []])
    await assert.rejects(f.call({ operation: 'pdf', output: 'merged.pdf', options: { action: 'merge', inputs } }, f.exec));
  assert.equal(f.state.calls.length, 1);
});

test('real DSH SessionStore and exact-session observation recognize native file refs', async t => {
  const f = await fixture(t), ctx = new Context();
  const { default: Sessions } = await native('dsh-session');
  const { default: Query } = await native('dsh-session-query-sqlite');
  const { createUserMessage } = await native('dsh-llm');
  const sessions = ctx.plugin(Sessions); await sessions.await();
  const query = ctx.plugin(Query, { path: ':memory:', openAt: 'never' }); await query.await();
  t.after(async () => { await query.dispose(); await sessions.dispose(); });
  const session = ctx.sessions.create('native-office-test', { meta: { cwd: f.root } });
  const id = attach(f, 'native-format-data'), part = f.state.events[0].data.content[0];
  session.append('user/message', createUserMessage({ source: { kind: 'user' }, content: [part] }), { surfaceOp: 'append' });
  f.services.sessionQuery = ctx.sessionQuery;
  const result = await f.call({ operation: 'attachments' }, { ...f.exec, agent: { session } });
  assert.equal(result.result.attachments[0].attachment_id, id);
  await f.call({ operation: 'read', attachment_id: id }, { ...f.exec, agent: { session } });
  const stranger = ctx.sessions.create('native-office-stranger', { meta: { cwd: f.root } });
  await assert.rejects(f.call({ operation: 'read', attachment_id: id }, { ...f.exec, agent: { session: stranger } }), /OWN_ATTACHMENT/);
});

test('real office engine creates reads and edits modern office files without changing originals', async t => {
  const f = await fixture(t);
  const call = createOfficeAdapter(f.face, { engineRoot: path.resolve(here, '../../../office-engine'), pythonExecutable });
  const caps = await call({ operation: 'capabilities' }, f.exec);
  assert.ok(caps.result.read_formats.includes('docx')); assert.ok(caps.result.read_formats.includes('xlsx'));
  await call({ operation: 'create', output: '测试合同.docx', options: { paragraphs: ['麦米办公测试', '原文123'] } }, f.exec);
  const before = await fs.readFile(path.join(f.root, '测试合同.docx'));
  const read = await call({ operation: 'read', input: '测试合同.docx' }, f.exec);
  assert.match(JSON.stringify(read.result), /麦米办公测试/);
  await call({ operation: 'edit', input: '测试合同.docx', output: '合同修订.docx', options: { replacements: [{ find: '原文123', replace: '新文456' }] } }, f.exec);
  assert.match(JSON.stringify((await call({ operation: 'read', input: '合同修订.docx' }, f.exec)).result), /新文456/);
  assert.deepEqual(await fs.readFile(path.join(f.root, '测试合同.docx')), before);
  await call({ operation: 'create', output: '数据.xlsx', options: { sheets: [{ name: '部门统计', rows: [['名称', '金额'], ['合成', 25]] }] } }, f.exec);
  assert.match(JSON.stringify((await call({ operation: 'read', input: '数据.xlsx' }, f.exec)).result), /合成/);
});
test('cancellation before dispatch creates no private files', async t => {
  const f = await fixture(t), id = attach(f, 'bytes'); f.controller.abort();
  await assert.rejects(f.call({ operation: 'read', attachment_id: id }, f.exec));
  assert.equal(f.state.calls.length, 0); assert.deepEqual(await fs.readdir(f.root), []);
});

async function engineFixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'megmeet-office-engine-test-'));
  await fs.writeFile(path.join(root, 'engine.py'), `import json,sys,time,os\nr=json.loads(sys.stdin.readline())\no=r.get('options',{})\nif o.get('sleep'): time.sleep(10)\nif o.get('flood'): print('x' * (3*1024*1024)); sys.exit(0)\nif o.get('bad'): print('{}'); sys.exit(0)\nif o.get('error'): print(json.dumps({'protocol':'megmeet-office-v1','ok':False,'error':{'code':'SYNTHETIC_REFUSAL','message':'unsupported synthetic format'}})); sys.exit(0)\nprint(json.dumps({'protocol':'megmeet-office-v1','ok':True,'result':{'operation':r['operation'],'envKeyPresent':'DEEPSEEK_API_KEY' in os.environ,'pythonPathPresent':'PYTHONPATH' in os.environ}}))\n`);
  return { root, config: engineConfig({ engineRoot: root, pythonExecutable, timeoutMs: 5000 }) };
}
test('real isolated Python transport returns JSON and does not inherit model/IAM environment', async t => {
  const { config } = await engineFixture(); const existed = Object.hasOwn(process.env, 'DEEPSEEK_API_KEY');
  if (!existed) process.env.DEEPSEEK_API_KEY = 'synthetic-do-not-inherit';
  t.after(() => { if (!existed) delete process.env.DEEPSEEK_API_KEY; });
  const result = await runEngine(config, { protocol: PROTOCOL, operation: 'capabilities' }, undefined,
    (executable, args, options) => {
      assert.deepEqual(args, ['-I', '-B', config.script]);
      return spawn(executable, args, options);
    });
  assert.equal(result.envKeyPresent, false); assert.equal(result.pythonPathPresent, false); assert.equal(result.operation, 'capabilities');
});
test('trusted interpreter invocation path is preserved for virtual environments', async t => {
  const { root } = await engineFixture();
  const linkedDirectory = path.join(root, 'trusted-python-environment');
  await fs.symlink(path.dirname(pythonExecutable), linkedDirectory, process.platform === 'win32' ? 'junction' : 'dir');
  t.after(async () => { await fs.unlink(linkedDirectory); await fs.rm(root, { recursive: true, force: true }); });
  const invoked = path.join(linkedDirectory, path.basename(pythonExecutable));
  const config = engineConfig({ engineRoot: root, pythonExecutable: invoked });
  assert.equal(config.python, invoked);
  const value = await runEngine(config, { protocol: PROTOCOL, operation: 'capabilities' });
  assert.equal(value.operation, 'capabilities');
});
test('invalid, oversized, refused and cancelled real engine responses fail without hanging', async () => {
  const { config } = await engineFixture();
  await assert.rejects(runEngine(config, { operation: 'capabilities', options: { bad: true } }), /RESPONSE_INVALID/);
  await assert.rejects(runEngine(config, { operation: 'capabilities', options: { flood: true } }), /RESPONSE_TOO_LARGE/);
  await assert.rejects(runEngine(config, { operation: 'capabilities', options: { error: true } }), error => error.code === 'SYNTHETIC_REFUSAL');
  const controller = new AbortController(), start = Date.now();
  const pending = runEngine(config, { operation: 'capabilities', options: { sleep: true } }, controller.signal);
  setTimeout(() => controller.abort(), 80); await assert.rejects(pending, /CANCELLED/); assert.ok(Date.now() - start < 6000);
});
test('native DSH defineTool accepts and executes registered schemas without core patches', async t => {
  const f = await fixture(t), engine = await engineFixture();
  registerOfficeTools(f.face, { engineRoot: engine.root, pythonExecutable }, defineTool);
  assert.deepEqual(f.state.registrations.map(t => t.name), ['office_capabilities', 'office_attachments', 'office_document']);
  const value = await f.state.registrations[0].execute({}, f.exec);
  assert.equal(value.result.operation, 'capabilities');
  await assert.rejects(f.state.registrations[2].execute({ operation: 'read', input: '../../outside.pdf' }, f.exec), /PATH/);
});

test('Host-wide office admission rejects parallel work and releases after cancellation or failure', async () => {
  const { config } = await engineFixture();
  const controller = new AbortController();
  const active = runEngine(config, { protocol: PROTOCOL, operation: 'capabilities', options: { sleep: true } }, controller.signal);
  const start = Date.now();
  await assert.rejects(runEngine(config, { protocol: PROTOCOL, operation: 'capabilities' }),
    error => error.code === 'OFFICE_BUSY' && /稍后重试/.test(error.message));
  assert.ok(Date.now() - start < 1000);
  controller.abort();
  await assert.rejects(active, /CANCELLED/);
  await assert.rejects(runEngine(config, { protocol: PROTOCOL, operation: 'capabilities', options: { bad: true } }), /RESPONSE_INVALID/);
  assert.equal((await runEngine(config, { protocol: PROTOCOL, operation: 'capabilities' })).operation, 'capabilities');
});
