'use strict';
const statusNode = document.querySelector('#status');
const login = document.querySelector('#login');
const local = document.querySelector('#local');
const logout = document.querySelector('#logout');
async function refresh() {
  const state = await window.megmeetEntry.status();
  const company = state.mode === 'company';
  document.querySelector('.tag').textContent = company ? '公司统一身份登录' : '本机开发验证 · 合成公司登录';
  document.querySelector('small').textContent = company
    ? '本机聊天记录和工作文件保存在电脑；文档处理、OCR 和预览需要连接公司服务器，按需临时上传本次所需文件。个人云端工作区的数据保存在公司服务器。模型按本人权限提供。'
    : '本开发环境使用模拟员工和模型，不连接公司真实 IAM 或 TokenKey。个人云端工作区与本地文件分开管理。';
  statusNode.textContent = state.message || (state.user ? `已登录：${state.user.display_name || state.user.user_no}\n本机历史按此账号保存；工作文件在你选择的文件夹中。` : '请先登录。');
  login.hidden = !!state.user; local.hidden = !state.user; logout.hidden = !state.user && !state.remembered;
  login.textContent = state.remembered ? '重试连接' : '使用公司账号登录';
  document.querySelector('#version').textContent = /^\d+\.\d+\.\d+$/.test(state.version || '') ? '麦米办公助手 · ' + state.version : '麦米办公助手';
  login.disabled = state.busy; local.disabled = state.busy;
}
login.addEventListener('click', async () => { login.disabled = true; statusNode.textContent = '正在打开系统浏览器…'; try { await window.megmeetEntry.login(); } catch { statusNode.textContent = '登录未完成，请重试。'; } await refresh(); });
local.addEventListener('click', async () => { local.disabled = true; try { await window.megmeetEntry.local(); } catch { statusNode.textContent = '工作区暂时无法打开，已保留登录，请稍后重试。'; local.disabled = false; } });
logout.addEventListener('click', async () => { await window.megmeetEntry.logout(); await refresh(); });
setInterval(() => { void refresh().catch(() => { statusNode.textContent = '暂时无法读取登录状态，请稍后重试。'; }); }, 1200);
void refresh().catch(() => { statusNode.textContent = '暂时无法读取登录状态，请稍后重试。'; });

document.querySelector('#updates').addEventListener('click', () => { void window.megmeetEntry.updates().catch(() => { statusNode.textContent = '更新窗口暂时无法打开，请从帮助菜单重试。'; }); });
