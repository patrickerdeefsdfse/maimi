// Retain the native controller and protocol. Only enterprise path ownership differs.
import {WorkspaceController} from '/opt/dsh/node_modules/@deepseek-ai/dsh-api-workspace-controller/lib/index.js';
import {RemoteError} from '/opt/dsh/node_modules/@deepseek-ai/dsh-typert-protocol/lib/index.js';
const denied=()=>new RemoteError('workspace/cloud-fixed','云端工作区不可更改。',{});
export default class CompanyWorkspaceController extends WorkspaceController {
  create(request) {
    if(request?.path!=='/workspace') return Promise.reject(denied());
    return super.create(request);
  }
  rename(request) {
    if(request?.title!=='云端工作区') return Promise.reject(denied());
    return super.rename(request);
  }
  delete() { return Promise.reject(denied()); }
  async initializeDefault() { return undefined; }
}
