import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {pathToFileURL} from 'node:url';

export const IM_VERSION='5.10.0';
export const DWS_VERSION='1.0.66';
export const name='maimi-desktop-dingtalk-workspace';
export const inject=['skills'];
function ordinaryFile(file) {
  const st=fs.lstatSync(file);
  if (!st.isFile() || st.isSymbolicLink()) throw Error('DESKTOP_CONNECTOR_FILE_REQUIRED');
  return fs.readFileSync(file);
}
export function desktopConnectorProfile({runtimeRoot,home,platform=process.platform}) {
  if (platform!=='win32') return {bundles:[],patch:[]};
  if (!path.isAbsolute(runtimeRoot) || !path.isAbsolute(home)) throw Error('DESKTOP_CONNECTOR_ROOT_REQUIRED');
  const pkg=JSON.parse(ordinaryFile(path.join(runtimeRoot,'node_modules','@xmanrui','dsh-im','package.json')));
  if (pkg.name!=='@xmanrui/dsh-im' || pkg.version!==IM_VERSION) throw Error('DESKTOP_IM_VERSION_MISMATCH');
  // The fresh profile points to the persistent employee-specific DSH_HOME.
  // No credentials or account IDs are copied into the profile or package.
  const entry=path.join(runtimeRoot,'node_modules','@xmanrui','dsh-im','lib','index.js');
  ordinaryFile(entry);
  return {bundles:[],patch:[{insert:[{id:'xmanrui-dsh-im',name:pathToFileURL(entry).href,config:{dshHome:home}}]}]};
}
export function verifyDws(root) {
  if (!path.isAbsolute(root)) throw Error('DWS_ROOT_REQUIRED');
  const manifest=JSON.parse(ordinaryFile(path.join(root,'manifest.json')));
  if (manifest.schema!=='maimi-desktop-dws/v1' || manifest.version!==DWS_VERSION || !Array.isArray(manifest.files) || manifest.files.length<20) throw Error('DWS_MANIFEST_INVALID');
  for (const item of manifest.files) {
    if (!/^[A-Za-z0-9_./+ -]+$/.test(item.path) || item.path.split('/').some(x=>!x||x==='..'||x==='.') || path.isAbsolute(item.path)) throw Error('DWS_MANIFEST_PATH');
    const data=ordinaryFile(path.join(root,...item.path.split('/')));
    if (data.length!==item.bytes || crypto.createHash('sha256').update(data).digest('hex')!==item.sha256) throw Error('DWS_RESOURCE_MISMATCH');
  }
  if (!manifest.files.some(x=>x.path==='bin/dws.exe') || !manifest.files.some(x=>x.path==='skills/SKILL.md')) throw Error('DWS_PAYLOAD_MISSING');
  return {binary:path.join(root,'bin','dws.exe'),skills:path.join(root,'skills')};
}
export function apply(ctx,{root,platform=process.platform}) {
  if (platform!=='win32') return;
  const {binary,skills}=verifyDws(root);
  ctx.skills.register({name:'dingtalk-workspace',source:'bundled',provider:'maimi-dws',
    description:'读取和处理本人已授权的钉钉文档、云盘、知识库、会议纪要、日历和待办。需要搜索或操作钉钉工作资料时自动使用；钉钉机器人和个人微信聊天连接在设置的消息连接中配置。',
    invocation:{modelInvocable:true,userInvocable:true},resourceBase:{kind:'directory',path:skills},
    content:`# 钉钉工作资料（官方 DWS）\n\n已随桌面端安装 DWS ${DWS_VERSION}。二进制路径：${JSON.stringify(binary)}。无需 npm 下载或另装全局命令。每次执行使用绝对路径。\n\n首先按需阅读本目录 SKILL.md，然后只读当前产品所需的 references/products 或 multi 技能，避免一次把全部资料加入上下文。可用子命令及参数以此固定版本 --help 和叶子 schema 为准。\n\n## 本人授权与账号选择\n\n公司登录不等于钉钉授权。首次需要登录时说明需要用户本人在官方浏览器完成授权；只有用户明确要求登录才运行 auth login，不代填账号密码，不收集、不显示令牌。仅检查状态可用 auth status --readonly --format json。账号已授权也必须确认其对应当前用户；多账号先让用户选择，所有业务命令显式带 --profile corpId:userId，不自动使用首个/default 账号。Windows DWS 凭据由当前 Windows 用户的 DPAPI/HKCU 保管，DSH_HOME 和 DWS_CONFIG_DIR 不是账号凭据隔离边界；本能力只用于个人桌面，不部署给多用户网页版，不设置 DWS_KEYCHAIN_TEST_NAMESPACE 伪装隔离。\n\n## 执行边界\n\n设置 DWS_NO_UPDATE_CHECK=1。只从用户授权的任务出发操作钉钉；发送消息、共享或删除数据遵循任务授权与官方确认要求。普通资料读取不需要启动机器人、事件监听器或自动化任务。只在明确请求持续监听时启动长期事件；不要把鉴权内容写到聊天、日志、遥测或文件附件中。诊断采用错误类别，避免 --verbose 将凭据或业务数据打印入支持报告。应用更新会保留 Windows 用户凭据和 DSH_HOME；注销公司账号不自动注销或删除用户独立授权的钉钉账号。\n\n## 消息连接\n\n需要从钉钉机器人或个人微信给麦米发任务时，在 DSH 设置中的 dsh-im 消息连接内配置。个人微信由用户本人扫码，钉钉由用户填写自己的应用配置。安装插件不代表已完成绑定；不要替用户创建绑定或向任何人发送测试消息。`});
}
