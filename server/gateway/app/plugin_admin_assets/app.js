'use strict';

const employeePattern = /^M[0-9]{8}$/;
function parseEmployees(text) {
  const tokens = String(text ?? '').trim().split(/[\s,;，；、]+/u).filter(Boolean);
  const employees = new Set();
  const invalid = new Set();
  for (const token of tokens) {
    const employee = token.toUpperCase();
    if (employeePattern.test(employee)) employees.add(employee);
    else invalid.add(token);
  }
  return {employees: [...employees], invalid: [...invalid]};
}

const el = id => document.getElementById(id);
const employeeInput = el('employee');
const statusText = el('status');
let generation = 0;
const selectedPlugins = new Map();
let targetEmployees = [];
let grantTotals = new Map();
let pendingBatch = null;
let batchRunning = false;
let policies = [];
let policyRevision = 0;

const editingPolicies = new Map();
const savingPolicies = new Set();
let catalogTarget = null;
let eventFilter = null;
let employee = '';
let plugins = [];
let panel = '';
let authorized = false;
let canManageRoles = false;
let roleResult = null;
let roleRequest = 0;
let viewRevision = 0;
let listRevision = 0;
let busy = false;
let versions = [];
let checks = new Map();

function clearCatalog() {
  employee = '';
  catalogTarget = null;
  grantTotals.clear();
  plugins = [];
  el('catalog').hidden = true;
  el('items').replaceChildren();
}
function accessError(response) {
  if (![401,403].includes(response.status)) return false;
  ++viewRevision;
  ++generation;
  ++listRevision;
  authorized = false;
  canManageRoles = false;
  resetRole();
  ++policyRevision;
  resetJobs();
  policies = [];
  editingPolicies.clear();
  targetEmployees = [];
  selectedPlugins.clear();
  cancelBatch();
  employeeInput.disabled = false;
  panel = '';
  document.body.dataset.view = 'locked';
  el('page-title').textContent = '企业插件管理';
  clearCatalog();
  versions = [];
  checks.clear();
  for (const id of ['versions','events','network-list']) el(id).replaceChildren();
  for (const id of ['sections','grants-panel','releases-panel','activity-panel','guide-panel','roles-panel','retry-load']) el(id).hidden = true;
  if (el('confirm-change').open) el('confirm-change').close('cancel');
  el('archive').value = '';
  employeeInput.value = '';
  el('activity-employee').value = '';
  el('login').hidden = response.status !== 401;
  statusText.textContent = response.status === 401 ? '请通过公司账号重新登录。' : '当前账号没有插件管理权限。';
  return true;
}
function node(tag, text, className) {
  const n = document.createElement(tag);
  n.textContent = text;
  if (className) n.className = className;
  return n;
}
function retainedGrant(plugin) {
  return plugin.state === 'withdrawn' && plugin.enabled === true;
}
function focusRowAction(listId, key, label) {
  if (document.activeElement !== document.body) return;
  const row = Array.from(el(listId).children).find(item => item.dataset.key === key);
  const buttons = row ? Array.from(row.querySelectorAll('button:enabled')) : [];
  const preferred = buttons.find(button => button.textContent === label);
  const fallback = listId === 'items' ? el('scope') : el('reload-releases');
  (preferred || buttons[0] || fallback).focus({preventScroll:true});
}

function render() {
  const query = el('search').value.trim().toLowerCase();
  const scope = el('scope').value;
  function matchesScope(p) {
    if (!targetEmployees.length || scope === 'all') return true;
    if (employee) return scope === 'retained' ? retainedGrant(p) : !retainedGrant(p) && p.effective === (scope === 'on');
    return (grantTotals.get(releaseKey(p))?.[scope] || 0) > 0;
  }
  const results = plugins.filter(p => (p.name + ' ' + p.plugin_key).toLowerCase().includes(query) && matchesScope(p));
  const enabled = plugins.filter(p => p.effective).length;
  const retained = plugins.filter(retainedGrant).length;
  el('count').textContent = employee
    ? '已开放 ' + enabled + ' 项 · 未开放 ' + (plugins.length-enabled-retained) + ' 项' + (retained ? ' · 停用后保留授权 ' + retained + ' 项' : '')
    : '共 ' + plugins.length + ' 项';
  el('scope').hidden = !targetEmployees.length;
  el('client-status-note').hidden = !employee;
  el('empty').hidden = results.length > 0;
  const rows = document.createDocumentFragment();
  for (const plugin of results) {
    const row = document.createElement('li');
    row.className = 'plugin';
    row.dataset.key = plugin.plugin_key + '/' + plugin.version;
    const info = document.createElement('div');
    const pick = node(authorized ? 'label' : 'strong', '', authorized ? 'pick-item' : 'plugin-name');
    if (authorized) {
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = selectedPlugins.has(row.dataset.key);
      checkbox.disabled = batchRunning;
      checkbox.setAttribute('aria-label', '选择 ' + plugin.name + ' ' + plugin.version);
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selectedPlugins.set(row.dataset.key, plugin);
        else selectedPlugins.delete(row.dataset.key);
        cancelBatch();
        syncBatch();
      });
      pick.append(checkbox);
    }
    pick.append(node('span',plugin.name));
    const kind = plugin.delivery === 'bundled_skill' ? '内置技能' : plugin.kind === 'native' ? '桌面插件' : plugin.kind === 'skill' ? '工作技能' : '';
    info.append(pick, node('small',[kind,'版本 ' + plugin.version,plugin.restricted ? '受岗位权限限制' : ''].filter(Boolean).join(' · ')));
    row.append(info);
    if (employee) {
      row.append(clientStatusPanel(plugin));
    } else {
      const platforms = (plugin.clients || []).map(client => CLIENTS[client]).filter(Boolean);
      if (platforms.length) info.append(node('small',platforms.join(' · '),'surfaces'));
    }
    if (targetEmployees.length > 1) {
      const totals = grantTotals.get(releaseKey(plugin));
      row.append(node('small','已开放 ' + (totals?.on || 0) + ' 人 · 未开放 ' + (totals?.off || 0) + ' 人'
        + (totals?.job_limited ? '（含岗位受限 ' + totals.job_limited + ' 人）' : '')
        + (totals?.retained ? ' · 停用后保留 ' + totals.retained + ' 人' : ''),'state'));
    }
    const linked = policies.filter(policy => matchesPolicy(plugin, policy));
    for (const policy of linked) row.append(policyControls(policy));
    rows.append(row);
  }
  el('items').replaceChildren(rows);
  const extras = policies.filter(policy => !plugins.some(plugin => matchesPolicy(plugin,policy))
    && (policy.name + ' ' + policy.skill).toLowerCase().includes(query));
  el('extra-network').hidden = !extras.length || (targetEmployees.length > 0 && scope !== 'all');
  el('network-list').replaceChildren(...extras.map(policy => {
    const row = node('li','','plugin');
    const info = node('div','');
    info.append(node('h3',policy.name), node('small','内置技能 · 仅管理沙箱'));
    row.append(info,policyControls(policy));
    return row;
  }));
  el('empty').hidden = Boolean(results.length || !el('extra-network').hidden);
  syncBatch();
}

async function load(target, notice = '') {
  const id = ++generation;
  clearCatalog();
  const employees = target ? target.split(',') : [];
  const individual = employees.length === 1 ? employees[0] : '';
  catalogTarget = target;
  el('login').hidden = true;
  statusText.textContent = '正在读取插件…';
  try {
    const bulk = employees.length > 1;
    const response = bulk
      ? await fetch('/v1/plugin-admin/catalog-preview', {method:'POST',cache:'no-store',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({employees})})
      : await fetch('/v1/plugin-admin/catalog?employee=' + encodeURIComponent(individual), {cache:'no-store'});
    if (id !== generation || accessError(response)) return;
    if (!response.ok) throw Error();
    const body = await response.json();
    if (id !== generation) return;
    if (!Array.isArray(body.items) || (bulk ? !Array.isArray(body.employees) ||
        body.employees.length !== employees.length || body.employees.some((value,i) => value !== employees[i])
        : body.employee !== individual)) throw Error();
    const combined = new Map(body.items.map(item=>[releaseKey(item),item]));
    employee = individual;
    targetEmployees = employees;
    grantTotals = new Map(bulk ? body.items.map(item=>[releaseKey(item),item.grant_totals]) : []);
    plugins = [...combined.values()];
    const manager = bulk || body.can_manage === true;
    if (!manager && authorized) {
      accessError({status:403});
      return load('');
    }
    const entering = document.body.dataset.view === 'locked';
    authorized = manager;
    if (!bulk) canManageRoles = body.can_manage_roles === true;
    el('sections').querySelector('[data-panel="roles"]').hidden = !canManageRoles;
    document.body.dataset.view = manager ? 'manager' : 'reader';
    el('retry-load').hidden = true;
    el('catalog').hidden = false;
    el('list-title').textContent = individual ? individual + ' 的插件授权' : '插件与技能';
    for (const key of selectedPlugins.keys()) {
      if (combined.has(key)) selectedPlugins.set(key, combined.get(key));
      else selectedPlugins.delete(key);
    }
    statusText.textContent = notice || (bulk ? '已选 ' + employees.length + ' 位员工。' : '');
    render();
    if (entering) openLocation(true);
  } catch {
    if (id === generation) {
      statusText.textContent = '暂时无法读取插件目录，请重试。';
      el('retry-load').hidden = false;
    }
  }
}

function cancelBatch() {
  pendingBatch = null;
  el('batch-confirmation').hidden = true;
}
function syncBatch() {
  const boxes = Array.from(el('items').querySelectorAll('.pick-item input'));
  const checked = boxes.filter(box => box.checked).length;
  el('select-visible').indeterminate = checked > 0 && checked < boxes.length;
  el('select-visible').checked = !!boxes.length && checked === boxes.length;
  el('select-visible').disabled = !boxes.length || batchRunning;
  el('batch-summary').textContent = targetEmployees.length + ' 位员工 · 已选 ' + selectedPlugins.size + ' 项';
  const disabled = batchRunning || !selectedPlugins.size || !targetEmployees.length;
  el('bulk-grant').disabled = disabled;
  el('bulk-revoke').disabled = disabled;
  employeeInput.disabled = batchRunning;
  el('lookup').querySelector('button').disabled = batchRunning;
}
el('select-visible').addEventListener('change', () => {
  const checked = el('select-visible').checked;
  for (const item of el('items').children) {
    if (checked) selectedPlugins.set(item.dataset.key,plugins.find(p=>releaseKey(p)===item.dataset.key));
    else selectedPlugins.delete(item.dataset.key);
  }
  cancelBatch();
  render();
});
function prepareBatch(enabled) {
  if (!authorized || batchRunning || !targetEmployees.length || !selectedPlugins.size) return;
  if (targetEmployees.length > 200 || selectedPlugins.size > 50 || targetEmployees.length * selectedPlugins.size > 5000) {
    statusText.textContent = '请分批提交，每批最多 200 位员工、50 个版本，员工与版本的组合不超过 5,000 项。';
    return;
  }
  const snapshot = {request_id:crypto.randomUUID(), enabled, employees:[...targetEmployees], plugins:[...selectedPlugins.values()]};
  pendingBatch = snapshot;
  const action = enabled ? '开通' : '收回';
  el('batch-confirm-title').textContent = enabled
    ? '为 ' + snapshot.employees.length + ' 位员工开通 ' + snapshot.plugins.length + ' 项权限？'
    : '收回 ' + snapshot.employees.length + ' 位员工的 ' + snapshot.plugins.length + ' 项权限？';
  el('batch-confirm-targets').textContent = snapshot.employees.join('、');
  el('batch-confirm-items').replaceChildren(...snapshot.plugins.map(p=>node('li',p.name + ' · ' + p.version)));
  el('batch-accept').textContent = '确认' + action;
  el('batch-confirmation').hidden = false;
  el('batch-accept').focus();
}
el('bulk-grant').addEventListener('click', ()=>prepareBatch(true));
el('bulk-revoke').addEventListener('click', ()=>prepareBatch(false));
el('batch-cancel').addEventListener('click', ()=>{ cancelBatch(); el('bulk-grant').focus(); });

el('lookup').addEventListener('submit', e => {
  e.preventDefault();
  el('scope').value = 'all';
  el('search').value = '';
  loadEnteredEmployee();
});
employeeInput.addEventListener('input', () => {
  ++generation;
  employeeInput.setCustomValidity('');
  targetEmployees = [];
  cancelBatch();
  clearCatalog();
  el('retry-load').hidden = true;
  statusText.textContent = '点击“查看授权”以加载当前工号。';
});
el('scope').addEventListener('change',render);
el('search').addEventListener('input',render);

const API = '/v1/plugin-admin';
const CLIENTS = {web:'网页端',desktop:'桌面端'};
const STATES = {draft:'草稿',published:'已发布',withdrawn:'已停用'};
const ACTIONS = {upload:'上传草稿',publish:'发布版本',withdraw:'停用版本',grant:'分发授权',revoke:'撤销授权',receipt:'客户端回执',web_network_review:'技能网络审核'};
const NETWORK_LABEL = {public:'默认联网',confidential:'保密工作区'};

function receiptLabel(value) {
  return ({installed:'已安装',removed:'已移除',failed:'处理失败'})[value] || value || '未知';
}
function formatTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString('zh-CN',{hour12:false});
}
function releaseKey(row) { return row.plugin_key + '/' + row.version; }
function packageKey(row) { return [releaseKey(row),row.sha256,row.state,row.updated_at].join('/'); }
function releaseUrl(row,action) {
  return API + '/releases/' + encodeURIComponent(row.plugin_key) + '/' + encodeURIComponent(row.version) + '/' + action;
}

async function request(url, payload) {
  const init = {cache:'no-store', credentials:'same-origin'};
  if (payload !== undefined) Object.assign(init,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const res = await fetch(url, init);
  const data = await res.json().catch(() => ({}));
  if (res.ok) return data;
  const reason = typeof data.detail === 'string' ? data.detail : '';
  if (res.status === 401 || (res.status === 403 && (!reason || ['plugin_admin_required','plugin_admin_origin_denied'].includes(reason)))) {
    accessError(res);
  }
  const err = new Error(reason);
  err.status = res.status;
  throw err;
}

function loadEnteredEmployee() {
  const parsed = parseEmployees(employeeInput.value);
  if (parsed.invalid.length) {
    employeeInput.setCustomValidity('工号格式应为 M 加 8 位数字。');
    employeeInput.reportValidity();
    statusText.textContent = '请核对工号：' + parsed.invalid.join('、');
    return;
  }
  if (parsed.employees.length > 200) {
    employeeInput.setCustomValidity('每次最多查询 200 位员工。');
    employeeInput.reportValidity();
    return;
  }
  return load(parsed.employees.join(','));
}

const panelLinks = Array.from(el('sections').querySelectorAll('[data-panel]'));
function openLocation(initial = false) {
  if (document.body.dataset.view === 'locked') return;
  const route = (location.hash.slice(1) || new URLSearchParams(location.search).get('panel') || 'grants').replace(/-panel$/, '');
  const link = (authorized && panelLinks.find(item => item.dataset.panel === route && !item.hidden)) || panelLinks[0];
  const name = link.dataset.panel;
  if (!initial && name === panel) return;
  if (panel==='grants') {
    stopJobsPolling();
    ++policyRevision;
    editingPolicies.clear();
    cancelBatch();
  }
  ++viewRevision;
  ++generation;
  ++listRevision;
  if (panel === 'roles') resetRole();
  panel = name;
  el('sections').hidden = !authorized;
  for (const item of panelLinks) {
    el(item.dataset.panel + '-panel').hidden = item !== link;
    if (item === link) item.setAttribute('aria-current','page');
    else item.removeAttribute('aria-current');
  }
  el('page-title').textContent = authorized ? '企业插件管理' : '企业插件目录';
  document.title = el('page-title').textContent + ' · 麦米办公助手';
  el('retry-load').hidden = true;
  if (el('confirm-change').open) el('confirm-change').close('cancel');
  if (!authorized) return;
  if (!initial) el('page-title').focus();
  if (name === 'grants') {
    policies = [];
    const loaded = Promise.all([loadNetwork(), refreshJobs(), initial ? undefined : loadEnteredEmployee()]);
    const version = viewRevision;
    if (route === 'network') loaded.then(() => {
      if (authorized && viewRevision === version && !el('catalog').hidden) el('network-scope-note').focus();
    });
    return;
  }
  statusText.textContent = '';
  if (name === 'releases') loadVersions();
  if (name === 'activity') loadEvents();
}
window.addEventListener('hashchange', () => openLocation());
function resetRole() {
  roleRequest++;
  roleResult = null;
  el('role-confirm').hidden = true;
  el('role-result').hidden = true;
  el('role-status').textContent = '';
}
const roleNames = {owner:'最高管理员', manager:'插件管理员', reader:'普通员工'};
function displayRole(data) {
  if (!Object.hasOwn(roleNames,data.role) || !Number.isSafeInteger(data.revision)) throw Error();
  roleResult = data;
  el('role-label').textContent = data.employee;
  el('role-value').textContent = roleNames[data.role];
  el('role-result').hidden = false;
  el('role-confirm').hidden = true;
  el('role-change').hidden = data.role === 'owner';
  el('role-change').textContent = data.role === 'manager' ? '收回管理员权限' : '授予管理员权限';
}
el('role-number').addEventListener('input', () => {
  el('role-number').value = el('role-number').value.toUpperCase();
  resetRole();
});
el('role-search').addEventListener('submit', async e => {
  e.preventDefault();
  if (!canManageRoles) return;
  resetRole();
  const number = el('role-number').value;
  const serial = roleRequest;
  try {
    const data = await request(API + '/roles/' + encodeURIComponent(number));
    if (serial !== roleRequest || !canManageRoles) return;
    if (data.employee !== number) throw Error();
    displayRole(data);
  } catch {
    if (serial === roleRequest) el('role-status').textContent = '暂时无法读取权限，请重新查询。';
  }
});
el('role-change').addEventListener('click', () => {
  if (!roleResult || !canManageRoles || roleResult.role === 'owner') return;
  el('role-change').hidden = true;
  el('role-question').textContent = '确认对 ' + roleResult.employee + ' ' + el('role-change').textContent + '？';
  el('role-confirm').hidden = false;
});
el('role-cancel').addEventListener('click', () => { if (roleResult) displayRole(roleResult); });
el('role-save').addEventListener('click', async () => {
  if (!canManageRoles || !roleResult || roleResult.role === 'owner' || el('role-confirm').hidden) return;
  const target = roleResult;
  const serial = ++roleRequest;
  el('role-confirm').hidden = true;
  try {
    const row = await request(API + '/roles', {employee:target.employee, enabled:target.role==='reader', expected_revision:target.revision});
    if (serial !== roleRequest || !canManageRoles) return;
    if (row.employee !== target.employee) throw Error();
    displayRole(row);
    el('role-status').textContent = '已保存。';
  } catch (error) {
    if (serial !== roleRequest) return;
    resetRole();
    el('role-status').textContent = error.status === 409 ? '权限已变更，请重新查询。' : '请重新查询该工号，核对当前权限。';
  }
});
el('retry-load').addEventListener('click', ()=>{
  if (!authorized) load('');
  else if (panel === 'grants') loadEnteredEmployee();
  else if (panel === 'releases') loadVersions();
  else if (panel === 'activity') loadEvents();
});

function actionButton(label,fn) {
  const button = node('button',label);
  button.type = 'button';
  button.addEventListener('click', fn);
  return button;
}
// Project skill names match the gateway's skill-network catalog.
const PROJECT_SKILLS = {meeting:'meeting-documents',heatcost:'drawing-cost-analysis',
  service:'server-operations','infrastructure-monitor':'infrastructure-monitor',
  'sales-orders':'sales-orders','sales-order-approval':'sales-order-approval'};
function matchesPolicy(plugin, policy) {
  if (plugin.delivery === 'bundled_skill') return plugin.skill === policy.skill;
  if (plugin.kind === 'skill' || plugin.kind === 'native') return policy.skill === 'company-' + plugin.plugin_key;
  return policy.project_key ? policy.project_key === plugin.plugin_key
    : PROJECT_SKILLS[plugin.plugin_key] === policy.skill;
}
function policyControls(policy) {
  const wrap = node('div','','inline-network');
  if (!Object.hasOwn(NETWORK_LABEL,policy.network_mode) || !Number.isSafeInteger(policy.revision)) {
    wrap.append(node('small','沙箱设置暂不可用'));
    return wrap;
  }
  const current = policy.network_mode;
  const next = current === 'public' ? 'confidential' : 'public';
  const draft = editingPolicies.get(policy.skill);
  const button = actionButton('沙箱保密', () => {
    editingPolicies.set(policy.skill,{revision:policy.revision,next});
    render();
    focusPolicy(policy.skill,'apply');
  });
  button.className = 'network-switch';
  button.dataset.skill = policy.skill;
  button.dataset.action = 'edit';
  button.setAttribute('role','switch');
  button.setAttribute('aria-checked', String(current === 'confidential'));
  button.setAttribute('aria-label','沙箱保密 · ' + policy.name);
  button.disabled = savingPolicies.has(policy.skill) || !!draft;
  wrap.append(button,node('span',NETWORK_LABEL[current],'state'));
  if (draft && draft.revision === policy.revision) {
    const confirm = node('div','','inline-confirm');
    confirm.append(node('p',draft.next==='confidential'
      ? '将所有员工和版本切换到离线沙箱，桌面端下次同步时撤下此技能。'
      : '所有已获授权的员工和版本将可在联网工作区使用此技能。'));
    const apply = actionButton(draft.next==='confidential' ? '确认设为保密' : '确认恢复联网',
      () => saveNetwork(policy,draft));
    const cancel = actionButton('取消', () => { editingPolicies.delete(policy.skill); render(); focusPolicy(policy.skill,'edit'); });
    apply.dataset.skill = policy.skill;
    apply.dataset.action = 'apply';
    apply.disabled = cancel.disabled = savingPolicies.has(policy.skill);
    confirm.append(apply,cancel);
    wrap.append(confirm);
  }
  return wrap;
}
function focusPolicy(skill, action) {
  const target = Array.from(el('catalog').querySelectorAll('button[data-skill]'))
    .find(button => button.dataset.skill === skill && button.dataset.action === action && !button.disabled);
  if (target) target.focus({preventScroll:true});
}
async function loadNetwork(notice = '') {
  const revision = ++policyRevision;
  el('reload-network').hidden = true;
  el('network-status').textContent = '正在读取沙箱设置…';
  try {
    const data = await request(API+'/skill-network');
    if (!authorized || revision !== policyRevision || panel !== 'grants') return;
    if (!Array.isArray(data.items)) throw Error();
    policies = data.items;
    for (const [key,draft] of editingPolicies) {
      if (!policies.some(p=>p.skill===key && p.revision===draft.revision)) editingPolicies.delete(key);
    }
    render();
    el('network-status').textContent = notice;
  } catch {
    if (!authorized || revision !== policyRevision || panel !== 'grants') return;
    policies = [];
    editingPolicies.clear();
    render();
    el('network-status').textContent = (notice ? notice + ' ' : '') + '沙箱设置暂不可用。';
    el('reload-network').hidden = false;
  }
}
async function saveNetwork(policy, draft) {
  if (!authorized || panel !== 'grants' || draft !== editingPolicies.get(policy.skill) || savingPolicies.has(policy.skill)) return;
  savingPolicies.add(policy.skill);
  render();
  let notice;
  try {
    await request(API+'/skill-network',{skill:policy.skill,expected_revision:draft.revision,network_mode:draft.next});
    notice = policy.name + '已设为' + NETWORK_LABEL[draft.next] + '。';
  } catch (error) {
    notice = error.status === 409 ? '配置已变化，请核对最新设置。' : '保存结果未确认，请核对当前设置。';
  } finally {
    savingPolicies.delete(policy.skill);
    editingPolicies.delete(policy.skill);
  }
  if (authorized && panel === 'grants') {
    await loadNetwork(notice);
    focusPolicy(policy.skill,'edit');
  }
}
el('reload-network').addEventListener('click',()=>loadNetwork());
function renderVersions() {
  const list = document.createDocumentFragment();
  for (const row of versions) {
    const item = node('li','','release-row');
    item.dataset.key = releaseKey(row);
    const heading = node('div','','release-head');
    heading.append(node('h3',row.name + ' · ' + row.version),node('span',STATES[row.state] || row.state,'state'));
    item.append(heading);
    const clients = (row.clients || []).map(v=>CLIENTS[v] || v).join('、');
    item.append(node('small',[row.plugin_key,row.kind === 'native' ? '原生插件' : '工作技能',clients].filter(Boolean).join(' · ')));
    if (row.description) item.append(node('p',row.description));
    const meta = node('details','');
    meta.append(node('summary','查看文件摘要'),node('code',row.sha256,'digest'));
    if (Number.isFinite(row.size_bytes)) meta.append(node('small',Math.ceil(row.size_bytes/1024) + ' KiB'));
    item.append(meta);
    const key = packageKey(row);
    const report = checks.get(key);
    if (report) {
      const notes = node('div','','readiness' + (report.ok ? '' : ' blocked'));
      notes.append(node('strong',report.ok ? '预检通过' : '预检未通过'));
      const entries = node('ul','');
      for (const check of report.checks) {
        entries.append(node('li',(check.ok ? '通过 · ' : '未通过 · ') + check.label + (check.message ? ' · ' + check.message : '')));
      }
      notes.append(entries);
      item.append(notes);
    }
    const actions = node('div','','commands');
    if (row.state === 'draft' || row.state === 'withdrawn') {
      actions.append(actionButton('预检',()=>runAction(row,'preflight')));
      const publish = actionButton(row.state === 'withdrawn' ? '重新发布' : '发布',()=>confirmAction(row,'publish'));
      publish.disabled = !report?.ok;
      if (publish.disabled) publish.title = '先完成该版本的预检';
      actions.append(publish);
    } else if (row.state === 'published') {
      actions.append(actionButton('停用',()=>confirmAction(row,'withdraw')));
    }
    if (busy) for (const button of actions.children) button.disabled = true;
    if (actions.childElementCount) item.append(actions);
    list.append(item);
  }
  el('versions').replaceChildren(list);
  el('versions-empty').hidden = versions.length !== 0;
  el('upload').querySelector('button').disabled = busy;
}
async function loadVersions(notice='') {
  const id = ++listRevision;
  versions = [];
  el('versions-empty').hidden = true;
  el('versions').replaceChildren();
  statusText.textContent = '正在读取插件版本…';
  try {
    const data = await request(API + '/releases');
    if (id !== listRevision) return;
    if (!Array.isArray(data.items)) throw Error();
    versions = data.items;
    const currentKeys = new Set(versions.map(packageKey));
    for (const key of checks.keys()) if (!currentKeys.has(key)) checks.delete(key);
    renderVersions();
    statusText.textContent = notice || '共 ' + versions.length + ' 个版本。';
    el('retry-load').hidden = true;
  } catch {
    if (id !== listRevision) return;
    versions = [];
    statusText.textContent = (notice ? notice + ' ' : '') + '暂时无法读取插件版本，请重试。';
    el('retry-load').hidden = false;
  }
}
el('reload-releases').addEventListener('click',()=>loadVersions());

async function refreshAfterRelease(notice) {
  if (!authorized) return;
  if (panel === 'releases') await loadVersions(notice);
  else if (panel === 'grants' && catalogTarget !== null) await load(catalogTarget);
  else if (panel === 'activity' && eventFilter !== null) await loadEvents();
}

function confirmAction(row,action) {
  if (busy) return;
  const publish = action === 'publish';
  const dialog = el('confirm-change');
  const revision = viewRevision;
  el('confirm-title').textContent = (publish ? '发布' : '停用') + '“' + row.name + '”？';
  el('confirm-detail').textContent = '版本 ' + row.version + (publish
    ? (row.state === 'withdrawn' ? ' 将重新分发给仍持有该版本授权的员工。' : ' 将进入员工授权目录。')
    : ' 将停止分发，客户端在后续同步时处理变更。');
  el('confirm-submit').textContent = publish ? '确认发布' : '确认停用';
  dialog.returnValue = 'cancel';
  dialog.addEventListener('close',()=>{
    if (dialog.returnValue === 'confirm' && revision === viewRevision) runAction(row,action);
  },{once:true});
  dialog.showModal();
}
async function runAction(row,action) {
  if (busy) return;
  const revision = viewRevision;
  const restoreFocus = el('versions').contains(document.activeElement);
  busy = true;
  renderVersions();
  statusText.textContent = '正在处理“' + row.name + '”…';
  let notice;
  try {
    const payload = {sha256:row.sha256};
    if (action !== 'preflight') Object.assign(payload,{expected_state:row.state,expected_updated_at:row.updated_at});
    const res = await request(releaseUrl(row,action),payload);
    if (action === 'preflight') {
      if (revision !== viewRevision) return;
      if (typeof res.ok !== 'boolean' || !Array.isArray(res.checks)) throw Error();
      if (versions.some(current => packageKey(current) === packageKey(row))) {
        checks.set(packageKey(row),res);
        notice = res.ok ? '预检通过，可以发布该版本。' : '预检未通过，请查看该版本的检查结果。';
      }
    } else {
      checks.delete(packageKey(row));
      await refreshAfterRelease(action === 'publish' ? '已发布，可在“员工授权”中分发。' : '版本已停用。');
    }
  } catch (e) {
    if (!authorized) return;
    checks.delete(packageKey(row));
    if (e.message === 'plugin_native_review_required') {
      notice = '此原生包的摘要尚未获得运维批准，请核对后重新预检。';
    } else if (e.status === 409 || e.status === 404) {
      await refreshAfterRelease('版本状态已发生变化，请核对最新记录。');
    } else {
      if (action !== 'preflight' && (!e.status || e.status >= 500)) {
        await refreshAfterRelease('未取得操作结果，请核对当前状态和操作记录。');
      } else {
        notice = '系统未接受这项操作，请重新预检并核对发布条件。';
      }
    }
  } finally {
    busy = false;
    if (authorized && panel === 'releases') {
      if (revision === viewRevision && notice) statusText.textContent = notice;
      if (versions.length || el('versions').children.length) renderVersions();
      el('upload').querySelector('button').disabled = false;
      if (restoreFocus && revision === viewRevision) focusRowAction('versions',releaseKey(row),action === 'preflight' ? '发布' : '');
    }
  }
}
function readBase64(file) {
  return new Promise((resolve,reject)=>{
    const reader = new FileReader();
    reader.onload=()=>resolve(reader.result.split(',')[1]);
    reader.onerror=()=>reject(Error('file_read'));
    reader.readAsDataURL(file);
  });
}
el('upload').addEventListener('submit',async e=>{
  e.preventDefault();
  if (busy || !authorized) return;
  const file = el('archive').files[0];
  if (!file || !/\.zip$/i.test(file.name) || !file.size || file.size > 4*1024*1024) {
    statusText.textContent = '请选择不超过 4 MiB 的非空 ZIP 文件。';
    return;
  }
  const revision = viewRevision;
  busy = true;
  renderVersions();
  statusText.textContent = '正在上传草稿…';
  try {
    const content = await readBase64(file);
    if (!authorized) return;
    const result = await request(API + '/releases',{filename:file.name,content_base64:content});
    if (el('archive').files[0] === file) el('archive').value = '';
    await refreshAfterRelease(result.state === 'draft' ? '草稿已保存，请预检后发布。'
      : '相同的文件已经存在，当前状态为' + (STATES[result.state] || '未知') + '。');
  } catch(e) {
    if (!authorized) return;
    if (e.message === 'plugin_key_reserved' || e.status === 413 || e.status === 422 || e.status === 400 || e.message === 'file_read') {
      if (revision !== viewRevision) return;
      if (e.message === 'plugin_key_reserved') statusText.textContent='此标识已由内置插件占用，请修改 manifest.json 的 plugin_key。';
      else if (e.message === 'file_read') statusText.textContent='浏览器未能读取这个文件，请重新选择。';
      else statusText.textContent='插件包未通过校验，请核对 ZIP 结构、清单和文件大小。';
    } else {
      await refreshAfterRelease(e.status === 409 ? '这个标识和版本已有记录，请核对内容或使用新的版本号。' : '上传结果未确认，请核对版本及操作记录。');
    }
  } finally {
    busy = false;
    if (authorized && panel === 'releases') {
      if (versions.length) renderVersions();
      el('upload').querySelector('button').disabled = false;
    }
  }
});

async function loadEvents() {
  const id = ++listRevision;
  const employee = el('activity-employee').value.trim().toUpperCase();
  el('events-empty').hidden = true;
  el('events').replaceChildren();
  if (!el('activity-employee').validity.valid) {
    eventFilter = null;
    statusText.textContent = '请补全员工工号（M 加 8 位数字），或清空工号后查询全部记录。';
    return;
  }
  eventFilter = employee;
  statusText.textContent = '正在读取操作记录…';
  try {
    const data = await request(API + '/activity?employee=' + encodeURIComponent(employee));
    if (id !== listRevision) return;
    if (!Array.isArray(data.items)) throw Error();
    const list=document.createDocumentFragment();
    for (const item of data.items) {
      const event=node('li','','event');
      const title = item.action === 'plugin_admin_role' ? (item.status==='granted'?'授予管理员权限':'收回管理员权限')
        : [ACTIONS[item.action] || item.action,item.plugin_key,item.version==='*'?'所有版本':item.version].filter(Boolean).join(' · ');
      event.append(node('h3',title));
      const time = node('time',formatTime(item.created_at));
      if (item.created_at) time.dateTime = item.created_at;
      event.append(time);
      const parts = [];
      if (item.actor) parts.push('操作人 ' + item.actor);
      if (item.employee) parts.push('员工 ' + item.employee);
      if (item.client) parts.push(CLIENTS[item.client] || item.client);
      if (item.status && item.action === 'receipt') parts.push(receiptLabel(item.status));
      if (item.status && item.action === 'web_network_review') parts.push(NETWORK_LABEL[item.status]);
      if (parts.length) event.append(node('p',parts.join(' · ')));
      list.append(event);
    }
    el('events').replaceChildren(list);
    el('events-empty').hidden = data.items.length !== 0;
    statusText.textContent = (employee ? employee+' · ' : '') + '本次查询返回 ' + data.items.length + ' 条。';
    el('retry-load').hidden = true;
  } catch {
    if (id !== listRevision) return;
    statusText.textContent = '暂时无法读取操作记录，请重试。';
    el('retry-load').hidden = false;
  }
}
el('activity-employee').addEventListener('input', e=>{
  ++listRevision;
  eventFilter = null;
  el('retry-load').hidden = true;
  e.target.value=e.target.value.toUpperCase();
  el('events').replaceChildren();
  el('events-empty').hidden=true;
  statusText.textContent='点击“查询记录”查看当前筛选结果。';
});
el('activity-lookup').addEventListener('submit',e=>{e.preventDefault();loadEvents()});

const BATCHES = API + '/batches';
const listPageSize = 20;
const itemPageSize = 50;
const jobStatus = {queued:'排队中', running:'执行中', completed:'已完成', completed_with_errors:'已完成，有失败项'};
const itemStatus = {pending:'待处理', changed:'已更新', unchanged:'已是目标状态', failed:'失败'};
const failureMessage = {
  plugin_release_not_found:'版本已停用或不存在，请先核对版本。',
  plugin_assignment_changed:'授权已发生变更，请核对员工当前授权。',
  plugin_admin_required:'执行时管理员权限已失效。',
  invalid_assignment:'工号或插件版本无效。',
  batch_item_failed:'服务处理失败。'
};
const receiptSync = {synced:'已同步', partial:'设备状态不一致', pending:'待同步', failed:'同步失败', not_applicable:'不适用'};
const receiptAvailability = {available:'可用', partial:'部分设备可用', unavailable:'不可用', unverified:'待验证', not_applicable:'不适用'};
let jobsRevision = 0;
let jobsTimer;
let listOffset = 0;
let itemOffset = 0;
let selectedJobId = '';
let latestJobs = [];
let shownJob = null;
let itemSignature = '';
let retrying = false;
const retryRequests = new Map();
const catalogJobs = new Set();

function clientStatusPanel(plugin) {
  const wrap = node('div','','client-status');
  const grant = plugin.authorization === 'granted' || (plugin.authorization == null && (plugin.effective || retainedGrant(plugin)));
  const permission = node('p','','authorization-line');
  permission.append(node('strong','员工授权'),node('span',retainedGrant(plugin) ? '已授权 · 版本已停用' : grant ? '已授权' : '未授权',
    grant ? 'state on' : 'state'));
  wrap.append(permission);
    if (plugin.job_limited && grant) permission.append(node('small','当前工号不在运维岗位范围内'));
  const table = document.createElement('table');
  table.setAttribute('aria-label',plugin.name + ' 客户端状态');
  table.setAttribute('aria-describedby','client-status-note');
  const headings = table.createTHead().insertRow();
  for (const label of ['客户端','设备同步','客户端可用性','最近回执']) {
    const th = node('th',label); th.scope = 'col'; headings.append(th);
  }
  const body = table.createTBody();
  for (const [key,label] of Object.entries(CLIENTS)) {
    const supported = !Array.isArray(plugin.clients) || plugin.clients.includes(key);
    const state = plugin.client_states?.[key] || {};
    const row = body.insertRow();
    const name = node('th',label); name.scope='row'; row.append(name);
    const deviceSummary = state.devices > 1 ? '（' + state.synced_devices + '/' + state.devices + ' 台已同步'
      + (state.failed_devices ? '，' + state.failed_devices + ' 台失败' : '') + '）' : '';
    row.append(node('td',(receiptSync[state.sync] || (supported ? '待同步' : '不适用')) + deviceSummary),
      node('td',receiptAvailability[state.availability] || (supported ? '待验证' : '不适用')),
      node('td',formatTime(state.at) || '—'));
  }
  wrap.append(table);
  return wrap;
}

function jobsOpen() {
  return authorized && panel === 'grants' && !document.hidden;
}
function stopJobsPolling() {
  ++jobsRevision;
  clearTimeout(jobsTimer);
}
function resetJobs() {
  stopJobsPolling();
  latestJobs = []; selectedJobId = ''; shownJob = null;
  itemSignature = ''; listOffset = 0; itemOffset = 0;
  retryRequests.clear();
  catalogJobs.clear();
  el('job-picker').hidden = true; el('job-detail').hidden = true;
  el('job-select').replaceChildren(); el('job-results').replaceChildren();
  el('jobs-note').textContent = '';
  el('job-export').removeAttribute('href');
}
function pageControls(prefix,offset,limit,total) {
  el(prefix + '-pages').hidden = total <= limit;
  el(prefix + '-prev').disabled = offset === 0;
  el(prefix + '-next').disabled = offset + limit >= total;
  el(prefix + '-range').textContent = (offset + 1) + '–' + Math.min(offset + limit,total) + ' / ' + total;
}
function isRunning(job) {
  return job.status === 'queued' || job.status === 'running';
}
function actionLabel(job) {
  return job.enabled ? '批量开通' : '批量收回';
}
function jobUrl(id) {
  return BATCHES + '/' + encodeURIComponent(id);
}
function renderJobList(data) {
  latestJobs = data.jobs;
  const select = el('job-select');
  const options = latestJobs.map(job => {
    const title = [formatTime(job.created_at), actionLabel(job), job.total + ' 项', jobStatus[job.status] || '状态未知',
      '编号 ' + job.id.slice(0,8)].join(' · ');
    return new Option(title,job.id);
  });
  const changed = select.options.length !== options.length || options.some((entry,i) =>
    select.options[i].value !== entry.value || select.options[i].text !== entry.text);
  if (changed) select.replaceChildren(...options);
  if (!latestJobs.some(job => job.id === selectedJobId)) {
    selectedJobId = latestJobs[0]?.id || '';
    itemOffset = 0;
  }
  select.value = selectedJobId;
  if (shownJob?.id !== selectedJobId) {
    shownJob = null;
    el('job-detail').hidden = true;
    el('job-export').removeAttribute('href');
  }
  el('job-picker').hidden = !latestJobs.length;
  pageControls('job-list',listOffset,listPageSize,data.total);
}
function renderJobDetail(data) {
  const job = data.job;
  shownJob = job;
  el('job-detail').hidden = false;
  el('job-title').textContent = actionLabel(job) + ' · ' + job.total + ' 项';
  el('job-state').textContent = jobStatus[job.status] || '状态未知';
  el('job-state').className = job.failed ? 'state job-warning' : job.status === 'completed' ? 'state on' : 'state';
  el('job-origin').hidden = !job.parent_id;
  el('job-origin').textContent = job.parent_id ? '重试自任务 ' + job.parent_id.slice(0,8) : '';
  const done = job.total - job.pending;
  el('job-progress').max = job.total || 1;
  el('job-progress').value = done;
  el('job-count').textContent = '进度 ' + done + ' / ' + job.total + ' · 已更新 ' + job.changed
    + ' · 已是目标状态 ' + job.unchanged + ' · 失败 ' + job.failed;
  el('job-export').href = jobUrl(job.id) + '/export.csv';
  el('job-retry').hidden = isRunning(job) || !job.failed;
  el('job-retry').disabled = retrying;
  const signature = JSON.stringify([job.id,itemOffset,data.items]);
  if (signature !== itemSignature) {
    itemSignature = signature;
    const rows = data.items.map(item => {
      const li = node('li','','job-result');
      const employeeButton = actionButton(item.employee,async () => {
        if (!authorized || batchRunning) return;
        cancelBatch();
        employeeInput.value = item.employee;
        el('scope').value = 'all'; el('search').value = item.plugin;
        await load(item.employee);
        if (employee === item.employee && panel === 'grants') el('list-title').scrollIntoView({block:'start'});
      });
      employeeButton.title = '查看这位员工的授权和客户端状态';
      employeeButton.setAttribute('aria-label',item.employee + '，查看授权和客户端状态');
      li.append(employeeButton,node('span',item.plugin + ' · ' + item.version),
        node('span',itemStatus[item.status] || '未知','receipt-state ' + item.status));
      if (item.error_code) li.append(node('small',failureMessage[item.error_code] || '处理失败（' + item.error_code + '）。'));
      return li;
    });
    el('job-results').replaceChildren(...rows);
  }
  pageControls('job-item',itemOffset,itemPageSize,data.total);
}

async function refreshJobs(preferredId = '') {
  stopJobsPolling();
  if (!jobsOpen()) return;
  const revision = jobsRevision;
  let wait = 0;
  try {
    const data = await request(BATCHES + '?offset=' + listOffset + '&limit=' + listPageSize);
    if (revision !== jobsRevision) return;
    if (!Array.isArray(data.jobs)) throw new Error('invalid_jobs');
    if (preferredId) selectedJobId = preferredId;
    renderJobList(data);
    if (!selectedJobId) {
      shownJob = null; el('job-detail').hidden = true;
      el('jobs-note').textContent = '暂无批量任务。';
      return;
    }
    const detail = await request(jobUrl(selectedJobId) + '?offset=' + itemOffset + '&limit=' + itemPageSize);
    if (revision !== jobsRevision) return;
    if (detail.job?.id !== selectedJobId || !Array.isArray(detail.items)) throw new Error('invalid_job');
    const completedWhileOpen = !isRunning(detail.job) && (catalogJobs.has(selectedJobId) || (shownJob?.id === selectedJobId && isRunning(shownJob)));
    renderJobDetail(detail);
    el('jobs-note').textContent = '';
    if (completedWhileOpen) {
      catalogJobs.delete(selectedJobId);
      if (catalogTarget !== null && targetEmployees.length && !pendingBatch) load(catalogTarget);
    }
    if (latestJobs.some(isRunning) || isRunning(detail.job)) wait = 2500;
  } catch {
    if (revision !== jobsRevision || !authorized) return;
    el('jobs-note').textContent = '暂时无法更新任务进度，稍后会再试。';
    wait = 10000;
  } finally {
    if (wait && revision === jobsRevision && jobsOpen()) jobsTimer = setTimeout(refreshJobs,wait);
  }
}

el('batch-accept').addEventListener('click',async () => {
  if (!authorized || !pendingBatch || batchRunning) return;
  const operation = pendingBatch;
  const originatingView = viewRevision;
  batchRunning = true;
  el('batch-accept').disabled = true;
  el('batch-cancel').disabled = true;
  render();
  try {
    const data = await request(BATCHES,{request_id:operation.request_id, enabled:operation.enabled,
      employees:operation.employees,plugins:operation.plugins.map(p => ({plugin:p.plugin_key,version:p.version}))});
    if (!data.job?.id) throw new Error('missing_job');
    if (!authorized || viewRevision !== originatingView) {
      if (jobsOpen()) refreshJobs();
      return;
    }
    cancelBatch();
    catalogJobs.add(data.job.id);
    el('jobs-note').textContent = '任务已提交。';
    itemOffset = 0; listOffset = 0;
    await refreshJobs(data.job.id);
    if (authorized && viewRevision === originatingView) el('jobs-heading').scrollIntoView({block:'nearest'});
  } catch (err) {
    if (!authorized || viewRevision !== originatingView) return;
    statusText.textContent = err.status === 422 ? '请核对员工、版本和提交数量。' :
      '提交结果未确认，请查看任务列表，也可以再点一次确认核对同一请求。';
    refreshJobs();
  } finally {
    batchRunning = false;
    el('batch-accept').disabled = false;
    el('batch-cancel').disabled = false;
    if (authorized) render();
  }
});
el('job-retry').addEventListener('click',async () => {
  const job = shownJob;
  if (!jobsOpen() || retrying || !job || isRunning(job) || !job.failed) return;
  retrying = true;
  el('job-retry').disabled = true;
  const originatingView = viewRevision;
  const key = retryRequests.get(job.id) || crypto.randomUUID();
  retryRequests.set(job.id,key);
  try {
    const data = await request(jobUrl(job.id) + '/retry',{request_id:key});
    if (!data.job?.id) throw new Error('missing_job');
    if (!authorized || viewRevision !== originatingView || selectedJobId !== job.id) return;
    catalogJobs.add(data.job.id);
    itemOffset = 0; listOffset = 0;
    await refreshJobs(data.job.id);
  } catch (err) {
    if (!authorized || viewRevision !== originatingView || selectedJobId !== job.id) return;
    const explanation = {batch_not_finished:'当前任务仍在运行。',batch_no_failed_items:'当前任务没有失败项。'};
    el('jobs-note').textContent = explanation[err.message] || '重试结果未确认，请刷新任务列表或重新点击核对。';
  } finally {
    retrying = false;
    el('job-retry').disabled = false;
  }
});
function switchJob() {
  selectedJobId = el('job-select').value;
  itemOffset = 0;
  shownJob = null;
  el('job-detail').hidden = true;
  refreshJobs();
}
el('job-select').addEventListener('change',switchJob);
el('refresh-jobs').addEventListener('click',() => refreshJobs());
for (const [suffix,factor] of [['prev',-1],['next',1]]) {
  el('job-list-' + suffix).addEventListener('click',() => {
    listOffset = Math.max(0,listOffset + factor * listPageSize);
    itemOffset = 0; selectedJobId = ''; shownJob = null;
    el('job-detail').hidden = true;
    refreshJobs();
  });
  el('job-item-' + suffix).addEventListener('click',() => {
    itemOffset = Math.max(0,itemOffset + factor * itemPageSize);
    refreshJobs();
  });
}
document.addEventListener('visibilitychange',() => {
  if (document.hidden) stopJobsPolling();
  else if (jobsOpen()) refreshJobs();
});
load('');

