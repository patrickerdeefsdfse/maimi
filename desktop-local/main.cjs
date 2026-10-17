'use strict';
const { app, BrowserWindow, Menu, dialog, ipcMain, session, shell, powerMonitor, safeStorage } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { isNativeUrl, isExternalHttps, isAllowedResource, isAllowedDownload, isAllowedClipboardWrite } = require('./policy.cjs');
const { startHost } = require('./backend.cjs');
const { signIn, signOut, renewSession, createSessionExpiry } = require('./auth.cjs');
const authApi = require('./auth.cjs');
const {createRememberedStore} = require('./remembered-store.cjs');
const {createDeviceSession} = require('./device-session.cjs');
const { ensureLocalGateway } = require('./gateway-dev.cjs');
const { DEVELOPMENT, MODE, APP_NAME } = require('./deployment.cjs');
const { createUpdater } = require('./updater.cjs');
const { createUpdateWindow, nativeUpdateHandler } = require('./update-window.cjs');
const { diagnosticLog } = require('./diagnostic-log.cjs');
const {installDesktopShortcuts}=require('./shortcut-keyboard.cjs');
let shortcuts;

app.setName(APP_NAME);
app.setAppUserModelId(DEVELOPMENT ? 'com.megmeet.ai.local' : 'com.megmeet.ai');
app.enableSandbox();
const lifecycleSmoke = DEVELOPMENT && process.argv.includes('--desktop-lifecycle-smoke');
const smoke = DEVELOPMENT && (lifecycleSmoke || process.argv.some(arg => /^--desktop-(?:brand-|chat-)?smoke$/.test(arg)));
const fixture = process.argv.includes('--fixture-b') ? 'b' : 'a';
if (smoke) app.setPath('userData', path.join(app.getPath('userData'), 'verification-windows-v2-' + fixture));
const entryUrl = pathToFileURL(path.join(__dirname, 'entry.html')).href;
const appIcon = app.isPackaged ? path.join(process.resourcesPath, 'app-icon.ico')
  : path.join(__dirname, 'assets', 'app-icon.ico');
let entryWindow, nativeWindow, auth, host, busy = false, message = '', log, quitting = false;
let generation = 0, loginAbort, hostAbort;
let sessionRetirement;
let deviceSession, recoveryInFlight, recoveryTimer;
const retainedDraftWindows = new Set();
let updater, updateStatus = 'idle', updateVersion, systemShutdown = false, updateQuitComplete = false;
let quitCleanupComplete = false, updateHandoffInFlight = false, manualUpdateRequested = false;
let updatePanel, updateUnexpectedFailure = false, updateMenuProgress;
const updateBusyStates = ['checking', 'connecting', 'downloading', 'verifying', 'installing'];
const updateLabels = { idle: '自动检查更新', checking: '正在检查更新…', connecting: '发现新版本，正在连接下载服务…', downloading: '正在下载更新…', verifying: '正在校验更新…',
  ready: '更新已准备好，点击更新并重启', current: '已是最新版本', unavailable: '暂时无法检查更新，稍后自动重试',
  installing: '上次更新正在安装，请稍候', 'install-failed': '上次更新未完成，可重新安装' };
const expiryMessage = '登录已过期，请重新登录。已保存的会话和文件不会删除；重新登录前，请在原聊天窗口复制保留未发送的草稿，之后重新选择附件。';
const sessionExpiry = createSessionExpiry({ renew: renewSession, onExpire: previous => { void expireSession(previous); } });
const safePreferences = { nodeIntegration: false, contextIsolation: true, sandbox: true,
  webSecurity: true, webviewTag: false, devTools: false, backgroundThrottling: false };
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function validateEntry(event) {
  if (!entryWindow || event.sender !== entryWindow.webContents || event.senderFrame !== entryWindow.webContents.mainFrame || event.senderFrame?.url !== entryUrl)
    throw Error('Untrusted desktop entry');
}
function state() { sessionExpiry.check(); return { busy, message, user: auth?.user,
  remembered: deviceSession?.status().remembered === true, mode: MODE, version: app.getVersion() }; }

function clearRecoveryTimer() { if (recoveryTimer) clearTimeout(recoveryTimer); recoveryTimer = undefined; }
function closeRetainedDrafts() { for (const window of retainedDraftWindows) if (!window.isDestroyed()) window.close(); retainedDraftWindows.clear(); }
function scheduleRecovery() {
  clearRecoveryTimer();
  const status = deviceSession?.status();
  if (quitting || !status || !status.remembered && !status.pendingRevocation) return;
  recoveryTimer = setTimeout(() => {
    recoveryTimer = undefined;
    if (!auth && deviceSession.status().remembered) void restoreRemembered({openWorkspace: true});
    else void deviceSession.drainRevocations(AbortSignal.timeout(10000)).catch(() => {}).finally(scheduleRecovery);
  }, 30000);
  recoveryTimer.unref?.();
}
async function restoreRemembered({openWorkspace = true} = {}) {
  if (recoveryInFlight) return recoveryInFlight;
  if (quitting || busy || auth || !deviceSession) return false;
  const epoch = ++generation;
  clearRecoveryTimer(); busy = true; message = '正在恢复此设备的登录…';
  const work = (async () => {
    try {
      const result = await deviceSession.restore();
      if (quitting || generation !== epoch) return false;
      if (result.status !== 'restored') {
        message = ['expired', 'denied'].includes(result.status) ? '此设备登录已失效，请重新登录。' : '';
        scheduleRecovery();
        return false;
      }
      auth = result.auth; sessionExpiry.arm(auth); message = ''; busy = false; menus();
      for (const window of retainedDraftWindows) if (!window.isDestroyed())
        window.setTitle('登录已恢复，请使用新窗口；此窗口保留未发送内容');
      if (openWorkspace) await openLocal().catch(() => {});
      return true;
    } catch (error) {
      if (!quitting && generation === epoch && error.name !== 'AbortError') {
        message = '暂时无法连接登录服务，已保留此设备登录；连接恢复后会自动重试。';
        scheduleRecovery();
      }
      return false;
    } finally {
      if (generation === epoch) {busy = false; menus();}
    }
  })();
  recoveryInFlight = work;
  try { return await work; } finally { if (recoveryInFlight === work) recoveryInFlight = undefined; }
}
function openEntry() {
  if (entryWindow && !entryWindow.isDestroyed()) { entryWindow.show(); entryWindow.focus(); return; }
  entryWindow = new BrowserWindow({ width: 600, height: 560, show: !smoke, title: DEVELOPMENT ? 'MEGMEET AI · 本地开发版' : 'MEGMEET AI',
    icon: appIcon,
    webPreferences: { ...safePreferences, preload: path.join(__dirname, 'preload.cjs'), partition: 'persist:megmeet-entry-v2' } });
  entryWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  entryWindow.webContents.on('will-navigate', (event, url) => { if (url !== entryUrl) event.preventDefault(); });
  entryWindow.on('closed', () => { entryWindow = undefined; });
  void entryWindow.loadURL(entryUrl);
}
async function login() {
  sessionExpiry.check();
  if (busy || auth) return state();
  if (deviceSession?.status().remembered) {
    await restoreRemembered({openWorkspace: true});
    if (auth || deviceSession.status().remembered) return state();
  }
  // An expired window is retained only so its unsent draft can be copied. The
  // explicit re-login click retires that view before adopting another account.
  if (nativeWindow) nativeWindow.close();
  closeRetainedDrafts(); clearRecoveryTimer();
  const epoch = ++generation;
  loginAbort = new AbortController();
  const signal = loginAbort.signal;
  busy = true; message = '正在准备登录…';
  try {
    await ensureLocalGateway({ log, signal });
    signal.throwIfAborted();
    const browser = smoke
      ? (url, options) => require('./fixture-browser.cjs').fixtureBrowser(url, fixture, options)
      : url => shell.openExternal(url);
    const options = {signal, onStatus: value => { if (generation === epoch) message = value; }};
    const result = deviceSession ? await deviceSession.login(browser, options) : {auth: await signIn(browser, options)};
    const candidate = result.auth;
    if (generation !== epoch || signal.aborted) {
      if (!candidate.remembered) await signOut(candidate).catch(() => {});
      throw Error('Login cancelled');
    }
    auth = candidate;
    sessionExpiry.arm(candidate);
    message = candidate.remembered ? '' : result.storageAvailable === false
      ? '已登录。本机安全存储暂不可用，本次关闭软件后需要重新登录。'
      : '已登录。当前服务器暂未提供记住设备登录，本次关闭软件后需要重新登录。';
  } catch { if (generation === epoch) message = DEVELOPMENT ? '登录未完成。请确认本机开发服务已启动，然后重试。' : '公司登录暂未完成，请稍后重试。'; throw Error('登录未完成。'); }
  finally { if (generation === epoch) busy = false; }
  return state();
}
async function external(url) {
  if (!isExternalHttps(url)) return;
  const result = await dialog.showMessageBox(nativeWindow, { type: 'question', title: '打开外部链接',
    message: '使用系统浏览器打开这个链接？', detail: new URL(url).origin,
    buttons: ['取消', '打开'], defaultId: 0, cancelId: 0 });
  if (result.response === 1) await shell.openExternal(url);
}
function updateSnapshot() {
  if (updateUnexpectedFailure) return { status: 'unavailable', currentVersion: app.getVersion(), canInstall: false, errorCode: 'check-unavailable' };
  return updater?.snapshot || { status: DEVELOPMENT ? 'disabled' : updateStatus === 'unavailable' ? 'unavailable' : 'disabled',
    currentVersion: app.getVersion(), canInstall: false, errorCode: undefined };
}
function startUpdates() {
  if (DEVELOPMENT || !app.isPackaged || process.platform !== 'win32') return;
  try {
    updater = createUpdater({ currentVersion: app.getVersion(), directory: path.join(app.getPath('userData'), 'software-updates'),
      policy: require('./update-policy.json'), onStatus: (value, version, detail, snapshot) => {
        const percent = value === 'downloading' && Number.isInteger(snapshot?.progress?.percent) ? snapshot.progress.percent : undefined;
        const changed = value !== updateStatus || version !== updateVersion || percent !== updateMenuProgress || updateUnexpectedFailure;
        if (value !== updateStatus) log?.write('SOFTWARE_UPDATE_STATE: ' + value + '\n');
        updateStatus = value; updateVersion = version; updateMenuProgress = percent; updateUnexpectedFailure = false;
        if (changed) menus();
      } });
    updater.start();
  } catch { updateStatus = 'unavailable'; menus(); log?.write('SOFTWARE_UPDATE_STATE: unavailable\n'); }
}
async function checkForUpdates() {
  try { await updater?.checkNow(); }
  catch { updateUnexpectedFailure = true; updateStatus = 'unavailable'; menus(); }
  return updateSnapshot();
}
function openUpdates() {
  updatePanel?.open();
  if (updater && !updateBusyStates.includes(updater.status)) void checkForUpdates();
  return { opened: !!updatePanel };
}
async function installUpdateNow() {
  if (!updater?.snapshot.canInstall || manualUpdateRequested || quitting) return { accepted: false };
  const result = await dialog.showMessageBox(updatePanel?.window || nativeWindow || entryWindow, {
    type: 'question', title: '安装软件更新', message: '退出麦米并安装已下载的更新？',
    detail: '请先完成正在运行的任务，并保存输入框中尚未发送的内容。已保存的聊天和文件会保留。安装时会显示进度，完成后自动打开新版本。',
    buttons: ['暂不安装', '更新并重启'], defaultId: 0, cancelId: 0,
  });
  if (result.response !== 1 || !updater?.snapshot.canInstall) return { accepted: false };
  manualUpdateRequested = true; setImmediate(() => app.quit()); return { accepted: true };
}
function menus() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(updater?.snapshot.canInstall ? [{ label: '更新并重启', click: () => { void installUpdateNow(); } }] : []),
    ...(shortcuts ? [shortcuts.fileMenu({fileMenu:"文件",closePage:"关闭当前页面"})] : []),
    { label: '账号与工作台', submenu: [
      { label: auth ? `当前账号：${auth.user.display_name || auth.user.user_no}` : '未登录', enabled: false },
      { label: '个人工作区：在原生界面选择本机文件夹', enabled: false },
      { label: '账号状态', click: () => openEntry() },
      { label: '停止插件与电脑操作', accelerator: 'CommandOrControl+Shift+Escape', click: () => { void host?.stopTools?.(); } },
      { label: '退出登录', click: () => { void logout(); } },
      { type: 'separator' }, { role: 'quit', label: '退出程序' },
    ] },
    { label: '编辑', submenu: [{ role: 'undo' }, { role: 'redo' }, { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' }] },
    { label: '视图', submenu: [{ role: 'reload' }, { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' }] },
    { label: '帮助', submenu: [
      { label: '麦米办公助手 ' + app.getVersion(), enabled: false },
      { label: DEVELOPMENT ? '本地开发版不接收公司更新' : (updateLabels[updateStatus] + (Number.isInteger(updateMenuProgress) ? ` ${updateMenuProgress}%` : '') + (updateVersion && updateStatus === 'ready' ? ` (${updateVersion})` : '')), enabled: false },
      { label: '软件更新…', click: () => { openUpdates(); } },
    ] },
  ]));
}
async function openLocal() {
  sessionExpiry.check();
  if (busy) throw Error('操作正在进行，请稍候。');
  if (!auth || auth.expiresAt <= Date.now()) throw Error('请重新登录。');
  if (nativeWindow && !nativeWindow.isDestroyed()) { nativeWindow.show(); nativeWindow.focus(); return; }
  busy = true; message = '正在打开本机 DSH…';
  const epoch = generation, owner = auth;
  hostAbort = new AbortController();
  const signal = hostAbort.signal;
  let openedHost, openedWindow;
  try {
    const candidate = host || await startHost({ auth: owner, stateRoot: path.join(app.getPath('userData'), 'personal-runtime'), log, signal,
      approveTool: async ({title,detail}) => {
        if(generation!==epoch||signal.aborted||!nativeWindow||nativeWindow.isDestroyed())return false;
        const result=await dialog.showMessageBox(nativeWindow,{type:'question',title,message:title,detail,
          buttons:['取消','允许本次操作'],defaultId:0,cancelId:0,noLink:true});
        return result.response===1&&generation===epoch&&!signal.aborted;
      } });
    if (generation !== epoch || signal.aborted) { await candidate.stop(); throw Error('Host opening cancelled'); }
    openedHost = candidate;
    host = candidate;
    const origin = host.origin;
    const nativeSession = session.fromPartition('persist:megmeet-personal-' + host.account);
    await nativeSession.setProxy({ mode: 'direct' });
    await nativeSession.clearCache();
    await nativeSession.cookies.set({ url: origin, name: host.browserCookie.name, value: host.browserCookie.value,
      path: '/', httpOnly: true, sameSite: 'strict', secure: false });
    if (generation !== epoch || signal.aborted) throw Error('Host opening cancelled');
    nativeSession.setPermissionRequestHandler((contents, permission, callback, details) => callback(
      contents === nativeWindow?.webContents
      && isAllowedClipboardWrite(permission, details.requestingUrl, details.isMainFrame, origin)));
    nativeSession.setPermissionCheckHandler((contents, permission, requestingOrigin, details) =>
      contents === nativeWindow?.webContents && requestingOrigin === origin
      && isAllowedClipboardWrite(permission, details.requestingUrl, details.isMainFrame, origin));
    nativeSession.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !isAllowedResource(details.url, origin) }));
    nativeSession.removeAllListeners('will-download');
    nativeSession.on('will-download', (event, item) => {
      if (!isAllowedDownload(item.getURL(), origin)) { event.preventDefault(); return; }
      item.setSaveDialogOptions({ title: '保存文件' });
    });
    nativeWindow = new BrowserWindow({ width: 1380, height: 920, minWidth: 900, minHeight: 620,
      show: !smoke, title: DEVELOPMENT ? 'MEGMEET AI · 本机个人工作区 · 开发版' : 'MEGMEET AI · 个人工作区',
      icon: appIcon,
      webPreferences: { ...safePreferences, session: nativeSession, preload: path.join(__dirname,'preload-native.cjs'), additionalArguments:['--megmeet-native-origin='+origin] } });
    openedWindow = nativeWindow;
    shortcuts.attach(nativeWindow);
    nativeWindow.webContents.setWindowOpenHandler(({ url }) => { void external(url); return { action: 'deny' }; });
    nativeWindow.on('page-title-updated', event => { event.preventDefault(); });
    for (const name of ['will-navigate', 'will-redirect']) nativeWindow.webContents.on(name, (event, url) => {
      if (!isNativeUrl(url, origin)) { event.preventDefault(); if (name === 'will-navigate') void external(url); }
    });
    nativeWindow.on('closed', () => { if (nativeWindow === openedWindow) nativeWindow = undefined; });
    await nativeWindow.loadURL(origin);
    if (generation !== epoch || signal.aborted) throw Error('Host opening cancelled');
    if (entryWindow) entryWindow.close();
    menus(); message = '';
  } catch (error) {
    // A failed opening must not leave a blank window that the next click treats
    // as a ready session. Retire only resources owned by this opening attempt.
    if (openedWindow && !openedWindow.isDestroyed()) openedWindow.close();
    if (nativeWindow === openedWindow) nativeWindow = undefined;
    if (host === openedHost) host = undefined;
    await openedHost?.stop();
    if (generation === epoch) {
      message = error?.code === 'NO_PERSONAL_MODELS'
        ? '当前账号没有可用模型，请联系管理员配置后重试。'
        : '个人工作区打开失败，请检查登录和公司服务后重试。';
    }
    throw Error(message || '个人工作区打开已取消。');
  } finally { if (generation === epoch) busy = false; }
}
async function expireSession(previous) {
  if (auth !== previous) return;
  const epoch = ++generation;
  sessionExpiry.clear(); loginAbort?.abort(); hostAbort?.abort();
  auth = undefined; busy = true; message = expiryMessage;
  const previousHost = host; host = undefined;
  // Stop authority immediately. Retain only the rendered window until the user
  // chooses login/logout; do not read, upload, erase or pretend to save drafts.
  openEntry(); menus();
  const canRestore = deviceSession?.status().remembered === true;
  if (canRestore && nativeWindow && !nativeWindow.isDestroyed()) {
    const oldWindow = nativeWindow; nativeWindow = undefined;
    retainedDraftWindows.add(oldWindow);
    oldWindow.setTitle('此前窗口：连接已暂停，保留未发送内容');
    oldWindow.on('closed', () => retainedDraftWindows.delete(oldWindow));
  }
  const retirement = Promise.allSettled([previousHost?.stop(),
    canRestore ? Promise.resolve() : signOut(previous, { timeout: 3000 })]);
  sessionRetirement = retirement;
  try { await retirement; }
  finally {
    if (sessionRetirement === retirement) sessionRetirement = undefined;
    if (generation === epoch) {
      busy = false; message = canRestore ? '正在恢复此设备的登录…' : expiryMessage; menus();
      if (canRestore) void restoreRemembered({openWorkspace: true});
    }
  }
}
async function logout() {
  sessionExpiry.clear();
  clearRecoveryTimer();
  ++generation; loginAbort?.abort(); hostAbort?.abort();
  busy = true;
  openEntry();
  nativeWindow?.close();
  closeRetainedDrafts();
  const previous = auth; auth = undefined;
  const previousHost = host; host = undefined;
  // DeviceSession persists its sign-out tombstone before any network wait.
  const revocation = (deviceSession ? deviceSession.forget(previous) : signOut(previous).then(() => true))
    .then(value => ({value}), error => ({error}));
  if (previousHost) await previousHost.stop();
  await sessionRetirement;
  const result = await revocation;
  message = result.error ? '退出尚未完成；请连接公司网络后重试退出。'
    : result.value ? '' : '本机已退出；连接恢复后会继续完成服务器设备撤销。';
  scheduleRecovery();
  busy = false; menus();
  return state();
}
async function verify() {
  await login(); await openLocal();
  let result;
  for (let n = 0; n < 90; n++) {
    result = await nativeWindow.webContents.executeJavaScript(`({
      title:document.title, hero:!!document.querySelector('img[data-megmeet-hero-image="r5"]'),
      sidebar:!!document.querySelector('img[data-megmeet-sidebar-image="r4"]'),
      headline:document.body.innerText.includes('电亮AI新纪元'),
      manualWorkspace:/选择工作区|选择文件夹|Choose workspace|Select a folder|Open workspace|Select workspace/i.test(document.body.innerText),
      nodeExposed:typeof require!=='undefined'||typeof process!=='undefined',
      text:document.body.innerText.slice(0,3000)
    })`);
    if (result.hero && result.sidebar) break;
    await delay(1000);
  }
  result.version = app.getVersion(); result.mode = 'windows-personal-development'; result.dsh = host.release.dsh;
  result.user = auth.user.user_no; result.statePath = host.dshHome;
  const evidence = path.join(app.getPath('userData'), 'evidence'); fs.mkdirSync(evidence, { recursive: true });
  fs.writeFileSync(path.join(evidence, 'desktop-smoke.json'), JSON.stringify(result, null, 2));
  await nativeWindow.webContents.executeJavaScript('new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))');
  await delay(400);
  const image = await nativeWindow.webContents.capturePage(undefined, { stayHidden: true, stayAwake: true }); fs.writeFileSync(path.join(evidence, 'desktop-smoke.png'), image.toPNG());
  if (!result.hero || !result.sidebar || !result.headline || result.nodeExposed) throw Error('Native desktop checks failed');
  log.write('WINDOWS_DESKTOP_SMOKE: PASS\n');
  await logout(); app.exit(0);
}
async function verifyLifecycle() {
  await login();
  const pendingHost = openLocal();
  await delay(50);
  await logout();
  await pendingHost.catch(() => {});
  if (host || auth || nativeWindow) throw Error('Logout allowed a late Host to reopen');
  const pendingLogin = login();
  await delay(5);
  await logout();
  await pendingLogin.catch(() => {});
  if (host || auth || nativeWindow) throw Error('Logout allowed a late login to restore identity');
  log.write('DESKTOP_LOGOUT_RACE_CHECK: PASS\n');
  diagnosticLog(process.stdout).write('DESKTOP_LOGOUT_RACE_CHECK: PASS\n');
  app.exit(0);
}
if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { const window = nativeWindow || entryWindow; window?.show(); window?.focus(); });
  app.whenReady().then(async () => {
    fs.mkdirSync(app.getPath('userData'), { recursive: true });
    log = diagnosticLog(fs.createWriteStream(path.join(app.getPath('userData'), 'desktop-startup.log'), { flags: 'a' }));
    deviceSession = createDeviceSession({store: createRememberedStore({
      directory: path.join(app.getPath('userData'), 'device-login'), safeStorage}), api: authApi});
    if (process.argv.includes('--desktop-chat-smoke')) throw Error('Use scripts/personal-runtime-smoke.cjs for native model and history verification; --desktop-chat-smoke is retired.');
    for (const [channel, handler] of Object.entries({ status: state, login, local: openLocal, logout, updates: openUpdates }))
      ipcMain.handle('entry:' + channel, async (event, ...args) => { validateEntry(event); if (args.length) throw Error('No entry arguments allowed'); return handler(); });
    shortcuts=installDesktopShortcuts(()=>nativeWindow,app.getPath('userData'),process.platform==='darwin'?'macos':process.platform==='win32'?'windows':'linux',menus,
      ()=>({revision:generation,blocked:busy||!auth||auth.expiresAt<=Date.now()}),
      url=>!!host&&!!auth&&auth.expiresAt>Date.now()&&isNativeUrl(url,host.origin));
    updatePanel = createUpdateWindow({ electron: require('electron'), icon: appIcon, preferences: safePreferences,
      snapshot: updateSnapshot, checkNow: checkForUpdates, installNow: installUpdateNow,
      parent: () => nativeWindow || entryWindow });
    ipcMain.handle('desktop:open-updates', nativeUpdateHandler({getWindow:()=>nativeWindow,getHost:()=>host,isNativeUrl,open:openUpdates}));
    menus(); openEntry(); startUpdates();
    powerMonitor.on('shutdown', () => { systemShutdown = true; updater?.stop(); });
    powerMonitor.on('resume', () => {
      if (quitting) return;
      updater?.resume();
      sessionExpiry.check();
      if (auth) void sessionExpiry.refresh();
      else void restoreRemembered({openWorkspace: true});
    });
    if (lifecycleSmoke) await verifyLifecycle();
    else if (smoke) await verify();
    else void restoreRemembered({openWorkspace: true});
  }).catch(async error => {
    log?.write('DESKTOP_START_FAILED: ' + String(error.message || error) + '\n');
    if (host) await host.stop();
    await deviceSession?.stop();
    if (!auth?.remembered) await signOut(auth, { timeout: 3000 }).catch(() => {});
    auth = undefined;
    if (!smoke) dialog.showErrorBox('MEGMEET AI', '启动未完成，请查看本机启动日志。');
    app.exit(1);
  });
}
// Closing the only entry window also cancels an in-progress browser login or
// Host start through before-quit; it must not strand an invisible signed-in app.
app.on('window-all-closed', () => { app.quit(); });
app.on('browser-window-created', (_event, window) => {
  window.on('query-session-end', () => { systemShutdown = true; updater?.stop(); });
});
app.on('before-quit', event => {
  if (quitCleanupComplete) return;
  if (quitting) { event.preventDefault(); return; }
  quitting = true;
  sessionExpiry.clear();
  clearRecoveryTimer();
  ++generation; loginAbort?.abort(); hostAbort?.abort();
  if (host || auth || sessionRetirement) {
    event.preventDefault(); quitting = true;
    const previous = auth, previousHost = host; auth = undefined; host = undefined;
    void Promise.allSettled([previousHost?.stop(), deviceSession?.stop(),
      previous?.remembered ? Promise.resolve() : signOut(previous, { timeout: 3000 }), sessionRetirement]).finally(() => {
      quitCleanupComplete = true; app.quit();
    });
  } else {
    const pendingDevice = deviceSession?.stop();
    if (pendingDevice) {
      event.preventDefault(); quitting = true;
      void pendingDevice.finally(() => {quitCleanupComplete = true; app.quit();});
    } else quitCleanupComplete = true;
  }
});
app.on('will-quit', event => {
  if (!updater || updateQuitComplete || !manualUpdateRequested) { updater?.stop(); shortcuts?.dispose(); shortcuts = undefined; return; }
  event.preventDefault();
  if (updateHandoffInFlight) return;
  updateHandoffInFlight = true;
  // Normal quit stops our Host; a persisted device login survives the updater.
  // Installer handoff rechecks signed metadata and bytes; NSIS waits for exit.
  const completeHandoff = started => {
    if (manualUpdateRequested && !systemShutdown && !started) {
      // Keep a usable entry/update window if an explicit installation cannot
      // launch. Recreate the stopped updater from its signed on-disk receipt.
      manualUpdateRequested = false; updateHandoffInFlight = false;
      quitting = false; quitCleanupComplete = false;
      deviceSession = createDeviceSession({store: createRememberedStore({
        directory: path.join(app.getPath('userData'), 'device-login'), safeStorage}), api: authApi});
      message = '更新安装未能启动。请在软件更新中查看原因并重试。';
      startUpdates(); openEntry(); openUpdates(); return;
    }
    updateQuitComplete = true; setImmediate(() => app.quit());
  };
  void Promise.resolve().then(() => updater.installAfterQuit({ systemShutdown, applicationPath: path.dirname(app.getPath('exe')) }))
    .then(completeHandoff, () => completeHandoff(false));
});
