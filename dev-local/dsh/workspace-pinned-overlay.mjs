// One reviewed native slot seam; never forge a remote path in the local Host.
import { createHash } from 'node:crypto';
export const workspaceSourceSha256 = 'cec5cdfc90b0732356b678e84ee00949d26b8c1b70d4dd5fcc20660d7267e448';
export function adaptPinnedWorkspace(source) {
  if (createHash('sha256').update(source).digest('hex') !== workspaceSourceSha256) throw Error('Review native workspace slot contract before upgrading DSH');
  const declaration = '\t\t\t\tchildren: { "sidebar.workspaces.directoryFlow": {';
  const render = '\t\t\t\t\t!wide && (0, react_jsx_runtime.jsx)("div", {\n\t\t\t\t\t\tclassName: WorkspaceBrowser_module_css_default.search,';
  const start = '\t\t\tstartSession(workspaceId) {\n';
  const empty = '\t\t\t\tif (target === void 0) {\n\t\t\t\t\tthis.clearMain();';
  const open = '\t\t\t\tthis.openWorkspace(target).catch((reason) => {\n\t\t\t\t\tconsole.warn("new session failed:", reason);\n\t\t\t\t});';
  if ([declaration, render, start, empty, open].some(anchor => source.split(anchor).length !== 2)) throw Error('Native workspace slot/navigation anchors changed');
  const status = `\t\t\tstartSession(workspaceId) {
                // Preserve upstream blank-draft reuse; make the completed action visible.
                const generation = this.megmeetNewGeneration = (this.megmeetNewGeneration || 0) + 1;
                const isCurrentNew = () => !this.lifetime.signal.aborted && this.megmeetNewGeneration === generation;
                const announce = text => {
                    if (!isCurrentNew()) return;
                    document.querySelector('[data-megmeet-new-conversation-status]')?.remove();
                    const node = document.createElement('div');
                    node.dataset.megmeetNewConversationStatus = '';
                    node.setAttribute('role', 'status'); node.setAttribute('aria-live', 'polite');
                    node.textContent = text;
                    Object.assign(node.style, { position: 'fixed', left: '50%', bottom: '16px', transform: 'translateX(-50%)', padding: '8px 14px', borderRadius: '8px', background: 'Canvas', color: 'CanvasText', border: '1px solid GrayText', zIndex: '1000', pointerEvents: 'none', fontSize: '13px' });
                    document.body.append(node); setTimeout(() => node.remove(), 5000);
                };
                const focusComposer = () => {
                    if (!isCurrentNew()) return;
                    announce('已打开新会话');
                    let attempts = 0;
                    const focus = () => {
                        if (!isCurrentNew()) return;
                        const editor = document.querySelector('[data-lexical-editor="true"][contenteditable="true"]');
                        if (editor && editor.getClientRects().length) { editor.focus({ preventScroll: true }); editor.scrollIntoView({ block: 'nearest' }); }
                        else if (++attempts < 30) setTimeout(focus, 100);
                    };
                    requestAnimationFrame(focus);
                };
                announce('正在打开新会话…');
`;
  return source.replace(declaration, '\t\t\t\tchildren: { "sidebar.workspaces.pinned": { kind: "single", scope: "root" }, "sidebar.workspaces.directoryFlow": {')
    .replace(render, '\t\t\t\t\trenderSlot("sidebar.workspaces.pinned", { wide, expandSidebar }),\n' + render)
    .replace(start, status)
    .replace(empty, '\t\t\t\tif (target === void 0) {\n                    announce("请先选择一个工作区，然后开始聊天。");\n\t\t\t\t\tthis.clearMain();')
    .replace(open, '\t\t\t\tthis.openWorkspace(target).then(focusComposer).catch((reason) => {\n                    announce("新会话暂时未打开，请重试。");\n\t\t\t\t\tconsole.warn("new session failed:", reason);\n\t\t\t\t});');
}
