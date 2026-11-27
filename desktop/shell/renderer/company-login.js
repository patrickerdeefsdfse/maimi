const entry = window.maimiEntry;
const element = (id) => document.getElementById(id);
const dialog = element('confirmation');
const phase = new URL(location.href).searchParams.get('state');
const retry = element('retry');
if (phase === 'unavailable' || phase === 'cancelled') {
  element('title').textContent = phase === 'cancelled' ? '已取消登录' : '暂时无法连接公司登录';
  element('detail').textContent = phase === 'cancelled' ? '准备好后，可在这里重新打开公司登录页。' : '请检查公司网络连接，然后重新登录。';
  retry.hidden = false;
}

async function refresh() {
  if (phase !== 'waiting') return;
  const state = await entry.action('state');
  if (!state || !/^[A-F0-9]{4}-[A-F0-9]{4}$/.test(state.code)) return;
  element('code').textContent = state.code;
  element('confirm-code').textContent = state.code;
  element('code-area').hidden = false;
  element('title').textContent = state.needsConfirmation ? '公司身份验证' : '等待浏览器确认';
  element('detail').textContent = state.needsConfirmation
    ? '完成身份验证后，麦米将打开你的工作区。'
    : '请在公司登录页核对验证码并确认，完成后返回麦米。';
  if (state.needsConfirmation && !dialog.open) dialog.showModal();
  if (!state.needsConfirmation && dialog.open) dialog.close();
}

let answering = false;
async function answer(action) {
  if (answering) return;
  answering = true;
  try {
    await entry.action(action, element('confirm-code').textContent);
    dialog.close();
    await refresh();
  } catch {
    dialog.close();
    element('detail').textContent = '本次登录确认已结束。';
  } finally { answering = false; }
}
dialog.addEventListener('cancel', (event) => { event.preventDefault(); void answer('cancel'); });
for (const name of ['authorize', 'cancel']) element(name).addEventListener('click', () => void answer(name));
for (const name of ['retry', 'update']) {
  const button = element(name);
  button.addEventListener('click', async () => {
    button.disabled = true;
    try { await entry.action(name); } finally { button.disabled = false; }
  });
}
entry.onChange(() => void refresh());
void refresh();
