# 部门云工作台与明确选择的企业任务

本模块不创建、迁移旧企业项目。`008_hybrid_workspaces.sql` 独立保存部门工作台、管理员授予、插件发布版本、附件、会话和运行记录。个人本地 DSH 的历史与文件不进入这些表；普通附件不触发企业路由。

## 身份与准入

所有 `/v1/cloud` 请求由 `desktop_auth.require_trusted_actor` 验证 IAM Cookie 或短期桌面 Bearer，当前同步目录提供稳定工号和全部有效组织。浏览器 Cookie 的变更请求还要求 Origin 与 `IAM_PUBLIC_ORIGIN` 精确一致。请求正文不能授予工号、组织、模型地址或 Key。

进入工作台必须同时满足当前 IAM 组织、工作台启用、该工作台的显式员工 grant。插件另外检查插件版本启用和员工的插件 grant。共享并不绕过这些权限。IAM 多部门不取第一项为默认组织，而是按被选工作台的实际组织匹配。

云会话默认私有；只有创建者可续写、修改可见性。创建者具有 `can_share` 才能发布到工作台；撤回为私有不要求保留分享权限。部门共享不等于其他员工可以继续改写同一人的会话。

## 当前 HTTP 契约

所有 ID 是 UUID，成功响应禁止缓存。

| 方法、路径 | 请求或响应 |
|---|---|
| GET `/v1/cloud/workspaces` | `items:[{workspace_id,name,organization_code,can_chat,can_share}]` |
| GET `/workspaces/{id}/plugins` | `items:[{plugin_key,version,name,simulation,model_policy}]` |
| POST `/workspaces/{id}/attachments` | 见下面上传契约 |
| GET `/attachments/{id}` | 当前授权后返回 `name,sha256,size_bytes,content_base64`，供用户明确下载 |
| POST `/workspaces/{id}/tasks` | `request_id,plugin_key,plugin_version,input,attachment_ids,conversation_id?` |
| POST `/workspaces/{id}/chats` | `request_id,input,attachment_ids,conversation_id?`，服务器选择工作台默认模型 |
| GET `/tasks/{run_id}` | 已授权运行的状态与结果 |
| GET `/workspaces/{id}/conversations` | 私有本人及获授权共享会话的目录，最多 100 条 |
| GET `/conversations/{id}` | `conversation_id,workspace_id,operation,plugin_key,plugin_version,visibility,owned,title,runs` |
| POST `/conversations/{id}/share` | `{visibility:"workspace"}` 发布；`private` 撤回 |

表中后续简写路径均以 `/v1/cloud` 为前缀。请求模型拒绝未声明字段。运行响应包含 `run_id,conversation_id,workspace_id,status,simulation,effective_model,result,error_code,input,attachment_ids,created_at,finished_at`。错误采用固定代码，不回显数据库异常、Key 或上游正文。

### 明确上传

```json
{
  "name": "selected.txt",
  "content_base64": "...",
  "sha256": "64位小写SHA256",
  "request_id": "本次云任务UUID",
  "operation": "plugin",
  "plugin_key": "synthetic-summary",
  "plugin_version": "1.0.0",
  "explicit_upload": true
}
```

首版仅 `.txt/.md/.csv/.json` 严格 UTF-8 文本，每个文件不超过 256 KiB；拒绝路径名、NUL、无效编码和摘要不匹配。没有 PDF、Excel、图片解析器。云普通聊天使用 `operation:"chat"`，不带插件字段。

上传响应含 `attachment_id,name,sha256,size_bytes,scope:{workspace_id,owner:"self",request_id}`。引用必须同时匹配员工、工作台、请求 ID、操作类别和插件版本，不能跨任务或跨插件复用。每轮最多 16 个附件，总输入限制 20 万字符。内容由长沙部署的 PostgreSQL BYTEA 保存；后续可替换为受控文件存储，外部引用契约不变。

本地 DSH receipt 只对原 Agent 有效，桌面适配器必须读取用户本轮明确选中的文件并上传换取云 attachment_id；不能把本地路径或 receipt 字符串直接当云文件 ID。用户只上传选定内容，不自动同步整个目录或个人历史。未使用的上传目前没有定时清理器，生产前需加入保留期及配额。

## 执行、上下文和计费

`synthetic-summary@1.0.0` 是服务端固定映射的验证插件，真实计算文本行数、字符数和前三行预览，不调用 LLM，不执行附件指令。响应明确 `simulation:true,model_called:false`。它不是语义总结能力或真实企业项目。

普通云聊天经 `personal_model_proxy.cloud_chat_complete`，使用当前可信员工身份取个人 Key，并以服务器生成的 run_id 标记调用。未知公司个人 Key API 未实现时仍拒绝，不回退共享 Key。模型来自工作台配置，并受模型 broker 的允许模型与个人凭据约束。前端不选择计费工号。

会话首次建立后固定工作台、员工、操作类别、插件和版本。每轮只读取同一会话的已完成上下文及明确上传的附件，不带入本地聊天、其他云会话或其他插件。当前模型窗口最多 19 条消息、20 万字符，超限移除最早完整往返；当前一轮超限则拒绝。

幂等键作用域为工作台、员工、request_id，并绑定规范化请求 SHA256；相同重试返回原运行，内容不同返回 409。PostgreSQL 事务锁处理同时重复请求，同一会话只允许一个运行中的任务。

已完成记录在数据库连接或应用进程重启后仍可查询。中断时遗留的 running 记录不会自动重放模型或业务调用，以免重复扣费/动作；当前没有后台恢复队列和中断清理器。此类任务需运维确认后终止，不能对用户宣称已自动恢复执行。

## 本地合成开通

应用迁移后，在本地 Gateway 容器中执行：

```sh
HYBRID_TEST_FIXTURES=true python -m app.enterprise_tasks seed-local
```

只生成固定合成 `LOCAL-TEST` 部门工作台、验证插件与 `LOCAL-FIXTURE-A` 的工作台/插件 grant，A 可分享；B 不自动开通。命令默认关闭、重复执行不扩大权限，不是生产管理员 API。正式工作台和授权应由受控管理流程写入相应表，当前未新增网页管理界面。

## 旧会话安全修复

旧 `history_store.record_chat_turn` 已禁止冲突写入改写会话组织、项目、插件和类型。个人会话调部门后继续归原员工；企业会话不能通过普通消息降级为个人。旧 `/v1/chat/run` 在调用 DSH 前还需执行 `assert_personal_conversation` 检查；旧附件字段不等于新上传桥。

SQL 回归使用独立随机合成 schema，测试员工/部门/工作台/插件/附件边界、私有与显式共享、作用域固定、幂等与重新连接持久化。它不替代公司网络、真实 TokenKey、容量、后台任务恢复和生产部署验收。
