const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const file = path.join(root, 'dev-local/dsh/enterprise-native.mjs');
let s = fs.readFileSync(file, 'utf8');
const start = s.indexOf('  function ModelRefresh() {'), end = s.indexOf('  async function submit(', start);
if (start < 0 || end < start) throw Error('ModelRefresh boundary differs');
s = s.slice(0, start) + `  // Hot settings updates feed the original DSH selector without touching drafts.
  let modelSync, modelSyncAt = 0, modelNotice = '';
  const modelObservers = new Set();
  function syncModels() {
    if (modelSync || Date.now() - modelSyncAt < 2000) return;
    if (workspaceContext?.get('connection')?.state?.getSnapshot() !== 'connected') return;
    modelSyncAt = Date.now();
    modelSync = request('/__megmeet/desktop/models/refresh', {})
      .then(value => { modelNotice = value.notice || ''; })
      .catch(() => { modelNotice = '模型列表暂未同步，请稍后重新打开窗口；发送时仍会核验模型权限。'; })
      .finally(() => { modelSync = undefined; modelObservers.forEach(fn => fn(modelNotice)); });
  }
  function ModelSync() {
    const [notice, setNotice] = useState(modelNotice);
    useEffect(() => {
      modelObservers.add(setNotice);
      const connection = workspaceContext?.get('connection')?.state;
      const onVisible = () => { if (document.visibilityState !== 'hidden') syncModels(); };
      const unsubscribe = connection?.subscribe(syncModels);
      window.addEventListener('focus', syncModels);
      document.addEventListener('visibilitychange', onVisible);
      const timer = setInterval(onVisible, 60000);
      syncModels();
      return () => {
        modelObservers.delete(setNotice); unsubscribe?.(); clearInterval(timer);
        window.removeEventListener('focus', syncModels);
        document.removeEventListener('visibilitychange', onVisible);
      };
    }, []);
    return notice ? h('small', { role: 'status', 'aria-label': notice, title: notice, 'data-megmeet-model-notice': '',
      style: { maxWidth: 130, minWidth: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', fontSize: 10 } }, notice) : null;
  }
` + s.slice(end);
s = s.replace("id: 'megmeet-model-refresh', order: 20 }, ModelRefresh)", "id: 'megmeet-model-sync', order: 20 }, ModelSync)");
s = s.replace('results.clear(); listeners.clear();', 'results.clear(); listeners.clear(); modelObservers.clear();');
fs.writeFileSync(file, s);
