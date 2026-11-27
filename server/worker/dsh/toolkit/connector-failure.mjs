// Private worker -> native connector contract. Expose only fixed local
// messages; provider errors can include URLs, input, or authentication data.
const known = new Map([
  ['PLUGIN_NOT_SELECTED', '尚未为当前任务选择该技能。请先查看企业工具目录并选择所需技能。'],
  ['PROJECT_CONFIDENTIAL_WORKSPACE_REQUIRED', '该技能需要保密工作区，请从麦米网页版进入保密工作区后继续。'],
  ['PROJECT_CONFIDENTIAL_OFFLINE', '保密任务全程离线，当前业务操作需要连接公司服务。'],
  ['CONFIDENTIAL_WORKSPACE_REQUIRED', '该技能需要保密工作区，请从麦米网页版进入保密工作区后继续。'],
  ['PROJECT_NOT_SELECTED', '请先选择当前任务所需的项目技能。'],
  ['PLUGIN_NOT_READY', '所选技能当前不可用，请检查企业工具目录中的状态。'],
  ['TOOL_NOT_AVAILABLE', '当前技能未提供这项操作，请查看目录中的可用操作。'],
  ['PROJECT_TOOL_REQUIRED', '该操作需要使用目录中指定的项目工具。'],
  ['PROJECT_CLOUD_REQUIRED', '该项目技能需要在云端工作区中使用。'],
  ['PLUGIN_SELECTION_CHANGED', '当前技能选择已变更，请按最新选择重新发送。'],
  ['TOOL_BUSY', '当前工具仍在处理上一项操作，请等待完成。'],
  ['TOOLKIT_SESSION_REQUIRED', '本次工具请求缺少有效的会话。'],
  ['CLOUD_NATIVE_SCOPE_REQUIRED', '本次工具请求未获当前任务授权。'],
]);
const statuses = new Map([
  [400, '本次工具请求的参数无效。'], [401, '登录授权已失效，请重新进入工作区。'],
  [403, '当前会话没有执行这项工具操作的权限。'], [404, '未找到所请求的工具或操作。'],
  [409, '工具状态已变更，请检查当前任务状态。'], [413, '本次工具请求过大。'],
  [422, '当前工具无法处理所提供的输入。'], [429, '工具请求过多，请稍后重试。'],
  [502, '工具后端未返回有效响应。'], [503, '工具服务暂时不可用，请稍后重试。'],
  [504, '等待工具服务响应超时。'],
]);

export function connectorFailure(error) {
  const raw = String(error?.code || error?.message || '').split(':', 1)[0];
  const scopeDenied = raw === 'CLOUD_NATIVE_SCOPE_REQUIRED' || raw === 'cloud task authorization required';
  const status = scopeDenied ? 403 : error?.loginExpired ? 401 : statuses.has(error?.status) ? error.status : 503;
  const detail = scopeDenied ? known.get('CLOUD_NATIVE_SCOPE_REQUIRED') : known.get(raw) || statuses.get(status);
  return {status, detail};
}
