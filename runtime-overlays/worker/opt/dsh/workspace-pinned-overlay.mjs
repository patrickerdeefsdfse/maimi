// Client presentation only. The authenticated Gateway remains the deletion
// authority. Unknown mode hides destructive/recovery actions until resolved.
export function createCloudHistoryPolicy(target = globalThis) {
  let mode = 'unknown', pending;
  const listeners = new Set();
  const refresh = () => {
    if (pending) return pending;
    pending = (async () => {
      try {
        const response = await target.fetch('/__megmeet/desktop/workspace', {
          credentials: 'same-origin', cache: 'no-store', redirect: 'error',
          signal: target.AbortSignal.timeout(5000),
        });
        if (!response.ok) throw Error('WORKSPACE_MODE_UNAVAILABLE');
        const value = await response.json();
        const next = value.mode === 'cloud' || value.mode === 'local' ? value.mode : 'unknown';
        if (next !== mode) { mode = next; for (const notify of listeners) notify(); }
      } catch {
        if (mode !== 'unknown') { mode = 'unknown'; for (const notify of listeners) notify(); }
      } finally { pending = undefined; }
      return mode;
    })();
    return pending;
  };
  return {
    getSnapshot: () => mode,
    subscribe: callback => { listeners.add(callback); return () => listeners.delete(callback); },
    refresh,
    mount: () => {
      void refresh();
      target.addEventListener('focus', refresh);
      return () => target.removeEventListener('focus', refresh);
    },
    requireLocal: async () => {
      if (await refresh() !== 'local') throw Error('云端历史已永久删除，无法恢复。');
    },
  };
}

export function adaptCloudHistoryUI(source, once, policySource) {
  source = once(source, 'let react = require("react");', `let react = require("react");
    const megmeetHistoryPolicy = (${policySource})(globalThis);
    const useMegmeetHistoryMode = () => react.useSyncExternalStore(megmeetHistoryPolicy.subscribe, megmeetHistoryPolicy.getSnapshot, megmeetHistoryPolicy.getSnapshot);`);
  source = once(source, 'function apply(ctx) {\n\t\t\tconst sessions', 'function apply(ctx) {\n\t\t\tctx.effect(() => megmeetHistoryPolicy.mount(), "megmeet: cloud history policy");\n\t\t\tconst sessions');
  source = once(source, 'async unarchiveSession(sessionId) {\n\t\t\t\tawait this.workspaces', 'async unarchiveSession(sessionId) {\n\t\t\t\tawait megmeetHistoryPolicy.requireLocal();\n\t\t\t\tawait this.workspaces');
  const replaceFunction = (name, next, transform) => {
    const start = source.indexOf('function ' + name + '(');
    const end = source.indexOf('function ' + next + '(', start);
    if (start < 0 || end < start) throw Error('HISTORY_UI_BOUNDARY:' + name);
    source = source.slice(0, start) + transform(source.slice(start, end)) + source.slice(end);
  };
  replaceFunction('ViewOptionsMenu', 'sessionDragOrder', part => {
    part = once(part, 'const [open, setOpen]', 'const historyMode = useMegmeetHistoryMode();\n\t\t\tconst [open, setOpen]');
    return once(part, '],\n\t\t\t\tselectedIds:', '].filter(item => historyMode === "local" || !["archived-filter-separator", "filter-by", "show-archived", "only-archived"].includes(item.id)),\n\t\t\t\tselectedIds:');
  });
  source = once(source, 'const archivedFilter = useStore((s) => s.archivedFilter ?? "default");', 'const historyMode = useMegmeetHistoryMode();\n\t\t\tconst savedArchivedFilter = useStore((s) => s.archivedFilter ?? "default");\n\t\t\tconst archivedFilter = historyMode === "local" ? savedArchivedFilter : "default";');
  for (const [name, next] of [['ArchiveSessionMenuItem', 'ArchiveSessionRowButton'], ['ArchiveSessionRowButton', 'SessionArchiveConfirmDialog']]) {
    replaceFunction(name, next, part => {
      part = once(part, 'const archived = useArchived((set) => set.has(sessionId));', 'const archived = useArchived((set) => set.has(sessionId));\n\t\t\tconst historyMode = useMegmeetHistoryMode();\n\t\t\tif (historyMode === "unknown" || (historyMode === "cloud" && archived)) return null;');
      const labels = ['t(archived ? "menu.unarchiveSession" : "menu.archiveSession")'];
      if (name === 'ArchiveSessionRowButton') labels.push('t(archived ? "actions.unarchive" : "actions.archive")');
      for (const label of labels) part = once(part, label, '(historyMode === "cloud" ? "永久删除对话" : ' + label + ')');
      return part;
    });
  }
  replaceFunction('ArchiveConfirmForm', 'activityLine', part => {
    part = once(part, 'const [archiving, setArchiving]', 'const historyMode = useMegmeetHistoryMode();\n\t\t\tconst [archiving, setArchiving]');
    part = once(part, 'title: t("archive.confirm.title")', 'title: historyMode === "cloud" ? "停止任务并永久删除对话" : t("archive.confirm.title")');
    part = once(part, 'description: t("archive.confirm.desc", { title: request.displayTitle })', 'description: historyMode === "cloud" ? `将停止“${request.displayTitle}”的任务并永久删除此对话，无法恢复。` : t("archive.confirm.desc", { title: request.displayTitle })');
    part = once(part, 'children: t("archive.confirm.action")', 'children: historyMode === "cloud" ? "停止并永久删除" : t("archive.confirm.action")');
    part = once(part, 'disabled: archiving,\n\t\t\t\t\tonClick: confirm,', 'disabled: archiving || historyMode === "unknown",\n\t\t\t\t\tonClick: confirm,');
    return part;
  });
  replaceFunction('RowActionToast', 'plainNoticeText', part => {
    part = once(part, 'const toast = useToast((current) => current);', 'const toast = useToast((current) => current);\n\t\t\tconst historyMode = useMegmeetHistoryMode();');
    part = once(part, 'const { sessionId } = toast;', 'const { sessionId } = toast;\n\t\t\t\tif (historyMode !== "local") return (0, react_jsx_runtime.jsx)(_deepseek_ai_dsh_client_ui_primitives.Toast, { text: historyMode === "cloud" ? "对话已永久删除" : "操作已完成", tone: "success", onDone: dismissToast }, `toast-${String(toast.seq)}`);');
    return part;
  });
  return source;
}
