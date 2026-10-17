import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync } from 'node:fs';
import vm from 'node:vm';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { adaptPinnedWorkspace } from './workspace-pinned-overlay.mjs';
const root = process.env.MEGMEET_CANDIDATE_RUNTIME ? pathToFileURL(path.resolve(process.env.MEGMEET_CANDIDATE_RUNTIME) + path.sep) : new URL('../../desktop-local/runtime/', import.meta.url);
const pristine = new URL('pristine/dsh-client-ui-workspace.js', root);
const source = readFileSync(existsSync(pristine) ? pristine : new URL('node_modules/@deepseek-ai/dsh-client-ui-workspace/lib/client.js', root), 'utf8');
const adapted = adaptPinnedWorkspace(source);
test('the exact upstream workspace module and reviewed adaptation remain executable syntax', () => {
  assert.doesNotThrow(() => new vm.Script(adapted));
  assert.throws(() => adaptPinnedWorkspace(source + '\n'), /Review/);
});
function fixture() {
  const statuses = []; let focused = 0, cleared = 0, opened = 0;
  const editor = { getClientRects: () => [1], focus: () => focused++, scrollIntoView() {} };
  const document = { querySelector: selector => selector.includes('lexical') ? editor : null,
    createElement: () => ({ dataset: {}, style: {}, setAttribute() {}, remove() {} }), body: { append: node => statuses.push(node.textContent) } };
  const begin = adapted.indexOf('startSession(workspaceId) {');
  const end = adapted.indexOf('\n\t\t\tasync archiveSession(', begin);
  const subject = vm.runInNewContext('({' + adapted.slice(begin, end) + '})', { document,
    setTimeout() {}, requestAnimationFrame: fn => fn(), recentWorkspace: () => undefined, console: { warn() {} } });
  subject.lifetime = { signal: { aborted: false } };
  subject.workspaces = { list: { getSnapshot: () => ({ phase: 'ready', items: [] }) } };
  subject.sessions = { list: { getSnapshot: () => ({ phase: 'ready', byId: {} }) }, clear: () => cleared++ };
  subject.clearMain = () => cleared++;
  subject.ctx = { layout: { selectPanel() {} } };
  subject.openWorkspace = async () => opened++;
  return { subject, statuses, values: () => ({ focused, cleared, opened }) };
}
test('global New gives loading/completion and focuses retained native draft without forcing a new session', async () => {
  const f = fixture(); f.subject.startSession('existing-workspace');
  assert.equal(f.statuses[0], '正在打开新会话…');
  await Promise.resolve();
  assert.deepEqual(f.values(), { focused: 1, cleared: 0, opened: 1 });
  assert.equal(f.statuses.at(-1), '已打开新会话');
});
test('no workspace and failed navigation are visible without fabricating a selected workspace', async () => {
  const empty = fixture(); empty.subject.startSession();
  assert.equal(empty.statuses.at(-1), '请先选择一个工作区，然后开始聊天。');
  assert.deepEqual(empty.values(), { focused: 0, cleared: 1, opened: 0 });
  const denied = fixture(); denied.subject.openWorkspace = async () => { throw Error('synthetic permission refusal'); };
  denied.subject.startSession('existing-workspace'); await Promise.resolve(); await Promise.resolve();
  assert.equal(denied.statuses.at(-1), '新会话暂时未打开，请重试。');
  assert.equal(denied.values().focused, 0);
});
test('a disposed or superseded navigation cannot focus stale workspace content', async () => {
  const f = fixture(); let finish;
  f.subject.openWorkspace = () => new Promise(resolve => { finish = resolve; });
  f.subject.startSession('workspace'); f.subject.lifetime.signal.aborted = true; finish();
  await Promise.resolve(); assert.equal(f.values().focused, 0);
});
