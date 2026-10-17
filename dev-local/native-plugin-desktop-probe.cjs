'use strict';
// Development-only Electron probe. Uses a fixed synthetic Windows directory,
// the real native DSH composer/file input and real local cloud-task endpoints.
// Never import this file from a packaged production entry point.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

// Windows packaged-app virtualization can expose the same physical directory
// through Roaming and Packages/.../LocalCache/Roaming paths. Compare resolved
// identity, never a basename or a path suffix that another directory can share.
function sameDirectory(left, right) {
  try {
    const a = fs.realpathSync.native(left), b = fs.realpathSync.native(right);
    const sa = fs.statSync(a, { bigint: true }), sb = fs.statSync(b, { bigint: true });
    if (!sa.isDirectory() || !sb.isDirectory()) return false;
    if (path.normalize(a).toLowerCase() === path.normalize(b).toLowerCase()) return true;
    return sa.ino > 0n && sb.ino > 0n && sa.dev === sb.dev && sa.ino === sb.ino;
  } catch { return false; }
}

module.exports = async function nativePluginDesktopProbe({ window, requestNative, user, evidence }) {
  assert.equal(process.platform, 'win32', 'This probe verifies the Windows native runtime');
  assert.equal(user?.user_no, 'LOCAL-FIXTURE-A', 'Only the synthetic granted employee may run this mutation probe');
  const wc = window.webContents, run = code => wc.executeJavaScript(code, true);
  const marker = 'SYNTHETIC_NATIVE_PLUGIN_' + crypto.randomUUID();
  const fixtureName = 'native-plugin-proof.txt';
  const folder = path.join(path.resolve(evidence), 'native-plugin-workspace');
  const fixture = path.join(folder, fixtureName);
  fs.mkdirSync(folder, { recursive: true });
  fs.writeFileSync(fixture, 'SYNTHETIC_NATIVE_PLUGIN_DOCUMENT\nSecond synthetic line\n', 'utf8');
  const checks = [], count = { uploads: 0, tasks: 0, ordinaryHttp: 0, ordinaryWebSocket: 0, confirmations: 0 };
  let ownsDebugger = false, dialogAccept = false, dialogFailure, workspace, session, task;
  const pass = name => { checks.push(name); console.log(name + ': PASS'); };
  async function wait(expression, label, tries = 100) {
    for (let n = 0; n < tries; n++) { if (await run(expression)) return; await delay(250); }
    throw Error(label);
  }
  async function waitMain(predicate, label, tries = 100) {
    for (let n = 0; n < tries; n++) { if (await predicate()) return; await delay(250); }
    throw Error(label);
  }
  async function capture(label) {
    await run('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
    fs.writeFileSync(path.join(evidence, label + '.png'),
      (await wc.capturePage(undefined, { stayHidden: true, stayAwake: true })).toPNG());
  }
  async function rpc(method, args) {
    const response = await requestNative('/api/' + method, { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ type: 'client-request', rpcId: crypto.randomUUID(), method, payload: { args } }),
      signal: AbortSignal.timeout(20000) });
    const result = await response.json();
    assert.equal(response.ok, true, 'Native RPC HTTP: ' + method);
    assert.equal(result.result?.ok, true, 'Native RPC result: ' + method);
    return result.result.value;
  }
  async function cloud(route) {
    const response = await requestNative('/__megmeet/enterprise' + route, { signal: AbortSignal.timeout(20000) });
    assert.equal(response.ok, true, 'Cloud read denied: ' + route.split('/')[1]);
    return response.json();
  }
  async function click(selector, label) {
    await wait(`(() => { const x=document.querySelector(${JSON.stringify(selector)}); return !!x&&!x.disabled; })()`, label);
    await run(`document.querySelector(${JSON.stringify(selector)}).click()`);
  }
  const sendSelector = 'button[aria-label="发送消息"],button[aria-label="Send message"]';
  const editorSelector = '[data-lexical-editor="true"][contenteditable="true"]';
  const onMessage = async (_event, method, params) => {
    // Keep only classification/counters; never log request headers or bodies.
    if (method === 'Network.requestWillBeSent') {
      const p = new URL(params.request.url).pathname;
      if (params.request.method === 'POST') {
        if (/^\/__megmeet\/enterprise\/workspaces\/[^/]+\/attachments$/.test(p)) count.uploads++;
        if (/^\/__megmeet\/enterprise\/workspaces\/[^/]+\/tasks$/.test(p)) count.tasks++;
        if (p === '/api/session/prompt' || /^\/__megmeet\/model\//.test(p)) count.ordinaryHttp++;
      }
    }
    if (method === 'Network.webSocketFrameSent') {
      const frame = params.response;
      const value = frame.opcode === 2 ? Buffer.from(frame.payloadData, 'base64').toString('utf8') : frame.payloadData;
      if (value.includes('session/prompt')) count.ordinaryWebSocket++;
    }
    if (method === 'Page.javascriptDialogOpening') {
      try {
        assert.equal(params.type, 'confirm');
        assert.equal(params.message.includes(fixtureName), true, 'Confirmation must name the actual local file');
        assert.equal(params.message.includes(workspace.name), true, 'Confirmation must name the destination');
        count.confirmations++;
        await wc.debugger.sendCommand('Page.handleJavaScriptDialog', { accept: dialogAccept });
      } catch {
        dialogFailure = 'Unexpected native upload confirmation';
        await wc.debugger.sendCommand('Page.handleJavaScriptDialog', { accept: false }).catch(() => {});
      }
    }
  };
  try {
    assert.equal(wc.debugger.isAttached(), false, 'Probe cannot replace another debugger');
    wc.debugger.attach('1.3'); ownsDebugger = true;
    wc.debugger.on('message', onMessage);
    await wc.debugger.sendCommand('Page.enable');
    await wc.debugger.sendCommand('Network.enable');
    await wc.debugger.sendCommand('DOM.enable');

    const registered = (await rpc('workspace/create', { request: { path: folder } })).workspace;
    assert.equal(sameDirectory(registered.path, folder), true, 'Native workspace must resolve to the actual fixture directory');
    // Host registration is synthetic setup; choosing/creating its conversation
    // below is the actual native sidebar UI, not a hidden JS service call.
    const labels = ['在“' + registered.title + '”中新建会话', 'New session in ' + registered.title];
    const newSessionSelector = labels.map(label => 'button[aria-label=' + JSON.stringify(label) + ']').join(',');
    await click(newSessionSelector, 'Native fixture workspace row did not appear');
    await waitMain(async () => {
      // Native DSH intentionally reuses an existing blank draft for this
      // workspace; a repeated probe must accept that documented behavior.
      session = (await rpc('session/list', { _request: {} })).items.find(x => x.blank
        && x.cwd && sameDirectory(x.cwd, folder));
      return !!session;
    }, 'Native workspace selection did not create a scoped session');
    assert.equal(session.blank, true);
    pass('NATIVE_PLUGIN_WINDOWS_WORKSPACE_SELECTED');

    await click('[data-megmeet-plugin-picker]', 'Native input plugin launcher missing');
    await wait('!!document.querySelector("[data-megmeet-cloud-panel]")', 'Native plugin catalog did not open');
    const authorized = (await cloud('/workspaces')).items;
    workspace = authorized.find(x => x.organization_code === 'LOCAL-TEST');
    assert.ok(workspace, 'Synthetic department grant missing');
    const wSelect = 'select[aria-label="选择部门云工作台"]';
    await wait(`Array.from(document.querySelector(${JSON.stringify(wSelect)})?.options||[]).some(o=>o.value===${JSON.stringify(workspace.workspace_id)})`, 'Granted workspace option missing');
    await run(`(() => { const e=document.querySelector(${JSON.stringify(wSelect)});e.value=${JSON.stringify(workspace.workspace_id)};e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    const pSelect = 'select[aria-label="选择企业插件"]';
    await wait(`Array.from(document.querySelector(${JSON.stringify(pSelect)})?.options||[]).some(o=>o.value==='synthetic-summary@1.0.0')`, 'Versioned synthetic plugin option missing');
    await run(`(() => { const e=document.querySelector(${JSON.stringify(pSelect)});e.value='synthetic-summary@1.0.0';e.dispatchEvent(new Event('change',{bubbles:true})); })()`);
    await wait(`Array.from(document.querySelectorAll('[data-megmeet-cloud-panel] button')).some(b=>b.textContent==='附加到原生聊天输入'&&!b.disabled)`, 'Attach plugin action unavailable');
    await run(`Array.from(document.querySelectorAll('[data-megmeet-cloud-panel] button')).find(b=>b.textContent==='附加到原生聊天输入').click()`);
    await wait('!document.querySelector("[data-megmeet-cloud-panel]")','Catalog did not close after native selection');
    await wait(`document.querySelector(${JSON.stringify(editorSelector)})?.textContent.includes('/企业任务')`, 'Native command claim was not inserted');
    pass('NATIVE_PLUGIN_SELECTED_IN_PERSONAL_COMPOSER');

    // Chromium supplies the real file through the upstream native hidden input.
    const root = await wc.debugger.sendCommand('DOM.getDocument');
    const input = await wc.debugger.sendCommand('DOM.querySelector', { nodeId: root.root.nodeId, selector: 'input[type="file"][hidden]' });
    assert.ok(input.nodeId, 'Native DSH file input missing');
    await wc.debugger.sendCommand('DOM.setFileInputFiles', { nodeId: input.nodeId, files: [fixture] });
    await wait(`document.body.innerText.includes(${JSON.stringify(fixtureName)})`, 'Native file attachment was not displayed');
    await run(`(() => { const e=document.querySelector(${JSON.stringify(editorSelector)});e.focus();const r=document.createRange();r.selectNodeContents(e);r.collapse(false);const s=window.getSelection();s.removeAllRanges();s.addRange(r); })()`);
    await wc.insertText(marker);
    await wait(`document.querySelector(${JSON.stringify(editorSelector)})?.textContent.includes(${JSON.stringify(marker)})`, 'Native composer did not accept synthetic text');
    await click(sendSelector, 'Native command send button not ready');
    await waitMain(() => count.confirmations === 1 || !!dialogFailure, 'Native upload confirmation was not shown');
    if (dialogFailure) throw Error(dialogFailure);
    await wait('document.body.innerText.includes("已取消上传")', 'Cancelled upload was not reported');
    assert.equal(count.uploads, 0); assert.equal(count.tasks, 0);
    pass('NATIVE_PLUGIN_CANCELLED_CONFIRMATION_NO_CLOUD_MUTATION');
    await capture('native-plugin-cancelled');

    dialogAccept = true;
    await click(sendSelector, 'Native retry send button not ready');
    await waitMain(() => count.confirmations === 2 || !!dialogFailure, 'Native retry confirmation missing');
    if (dialogFailure) throw Error(dialogFailure);
    await wait('document.body.innerText.includes("完整结果已保存在部门云工作台")', 'Native plugin completion notice missing', 160);
    await waitMain(async () => {
      const items = (await cloud(`/workspaces/${workspace.workspace_id}/conversations`)).items;
      const found = items.find(c => c.title === marker);
      if (!found) return false;
      task = await cloud(`/conversations/${found.conversation_id}`);
      return task.runs.some(r => r.input === marker && r.status === 'completed');
    }, 'Native action did not persist the expected cloud task');
    assert.equal(task.operation, 'plugin'); assert.equal(task.plugin_key, 'synthetic-summary');
    assert.equal(task.plugin_version, '1.0.0'); assert.equal(task.visibility, 'private'); assert.equal(task.owned, true);
    const matching = task.runs.filter(r => r.input === marker);
    assert.equal(matching.length, 1); assert.equal(matching[0].simulation, true);
    assert.equal(matching[0].attachment_ids.length, 1);
    assert.equal(count.uploads, 1); assert.equal(count.tasks, 1);
    pass('NATIVE_PLUGIN_CONFIRMED_REAL_ATTACHMENT_AND_CLOUD_RESULT');

    await delay(700);
    const after = (await rpc('session/list', { _request: {} })).items.find(x => (x.sessionId || x.id) === (session.sessionId || session.id));
    assert.equal(after?.blank, true, 'A plugin command must not create a normal model turn');
    assert.equal(count.ordinaryHttp, 0); assert.equal(count.ordinaryWebSocket, 0);
    pass('NATIVE_PLUGIN_NO_ORDINARY_MODEL_PROMPT_OR_PERSONAL_TURN');
    await capture('native-plugin-completed');
    fs.writeFileSync(path.join(evidence, 'native-plugin-ui.json'), JSON.stringify({
      scope: 'synthetic Windows native composer, real local IAM/cloud service; not production',
      user: user.user_no, workspacePath: folder, cloudWorkspace: workspace.workspace_id,
      conversationId: task.conversation_id, checks, count, passed: true,
    }, null, 2));
    return { checks, count, passed: true };
  } catch (error) {
    await capture('native-plugin-failure').catch(() => {});
    // This probe admits only the synthetic account; avoid saving arbitrary
    // renderer text/network payloads even on an unexpected failure.
    fs.writeFileSync(path.join(evidence, 'native-plugin-failure.json'), JSON.stringify({ checks, count,
      error: error.message, passed: false }, null, 2));
    throw error;
  } finally {
    if (ownsDebugger) { wc.debugger.removeListener('message', onMessage); wc.debugger.detach(); }
  }
};

module.exports.sameDirectory = sameDirectory;
