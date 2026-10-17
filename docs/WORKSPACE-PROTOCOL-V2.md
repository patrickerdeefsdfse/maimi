# MEGMEET 工作区协议 v2（本地实现）

协议与 DSH 前端/SDK 版本分开。客户端先请求 `GET /v1/cloud/capabilities`，要求
`protocol=megmeet-workspaces`、`version=2`，再启用下面的功能；不兼容时停止新功能并显示升级要求。

## 三种存储边界

- 个人：继续由原生 DSH 手选本机目录，本机保存普通对话、文件与工具状态，本模块不扫描或同步个人目录。
- 云端：部门和跨部门小组都由管理员预建。云对话默认仅创建者可读，主动共享后仍复核当前成员、插件和文档权限。
- 协同：用户本轮明确选定的本地文本附件和云资料版本进入独立云任务。个人完整会话不会自动上传；返回结果明确标记服务器存储，导出到本地需再次明确操作。

工作区共享的是授权资料；每次任务使用调用员工自己的可信 IAM 身份和个人模型代理，独立对话上下文。没有多人共用的可变 DSH Agent 进程。

## 成员与资料权限

部门工作区要求 **当前 IAM 部门匹配 + 显式成员授权**；小组要求显式成员授权，可跨部门。
没有登录自动开通、客户端自报工号、前端隐藏代替后端权限。`can_chat`、`can_share`、
`can_publish`、`can_retrieve` 分别授权；插件另需精确插件 ID/版本的授权。

文档默认私有。共享文档对授权成员可读，具有发布权限的成员可编辑；只有创建者能改变文档可见性。
每次修改产生不可变版本，`expected_version` 不匹配返回 409；历史版本正文不能原地更新。
撤回资料、改为私有或撤销成员/资料权限，会使相应旧版本及基于这些版本的云对话/结果也不可读。
模型执行完毕重新验证真实身份、当前成员及资料权限；权限改变时丢弃响应，不持久化模型答案。

已经由用户明确下载到本地的文件或此前已看见的内容无法远程收回；服务器权限撤回限制后续访问。

## API

全部路径以 `/v1/cloud` 开头，使用已有桌面应用凭据；Cookie 写请求还要求当前合法 Origin。

| 接口 | 请求/返回重点 |
|---|---|
| GET `/capabilities` | 协议 v2、功能清单、存储边界 |
| GET `/workspaces` | 当前可用工作区，`kind=department/group`，能力布尔值 |
| GET `/workspaces/{id}/documents` | 当前授权目录；不返回正文；最多 200 项 |
| POST `/workspaces/{id}/documents` | `name,content,visibility,explicit_upload:true`；版本从 1 开始 |
| GET `/documents/{id}?version=N` | 明确版本的正文/摘要/权限；省略版本读取当前版本 |
| PUT `/documents/{id}` | `expected_version,name,content,visibility,explicit_upload:true` |
| POST `/workspaces/{id}/retrieve` | `query,limit`；最多 20 项，授权内摘要及精确版本引用 |
| POST `/workspaces/{id}/tasks` | 原有插件任务 + `document_refs:[{document_id,version}]`；引用不空必须 `explicit_cloud_context:true` |
| POST `/workspaces/{id}/chats` | 同样支持明确版本引用；仍是独立服务器云会话 |
| GET `/tasks/{id}` | 任务结果、版本引用、固定 runner manifest、存储位置 |
| GET `/conversations/{id}?before=RUN_UUID` | 每页最多 50 轮，按时间正序返回；`has_more` 与 `next_before` 表示还有更早记录 |

已有 `/attachments` 上传是明确单文件动作，绑定员工、工作区、请求 ID 和插件版本；不能引用另一员工或另一次请求的上传。
UTF-8 txt/md/csv/json 附件或文本资料限 256 KiB，最多 16 个资料引用及 16 个附件，总模型上下文另限 200,000 字符。
禁止路径或 URL 自动抓取、二进制解析、任意脚本运行。客户端未知字段会被拒绝。

云会话历史不删除，初次只取最近 50 轮；用户可继续加载更早页。游标必须属于当前已授权会话。
模型上下文在 SQL 查询时只取最近 50 个已完成轮次，随后再按 18 条消息及 200,000 字符裁剪。
服务器对整段会话的历史资料依赖做 SQL 存在性检查，不把所有历史引用读到进程内存；旧资料撤权仍会保护派生答案。

每个工作区的**所有文档版本正文 + 明确上传附件**默认合计 256 MiB，可通过
`CLOUD_WORKSPACE_STORAGE_LIMIT_BYTES` 配置为 1 字节至 10 GiB。写入持有工作区事务锁，
并发文档与附件不会超配；重复上传同一幂等附件不再计费。旧版本与已撤回资料仍占存储，不自动删除。
额度不包含聊天结果及数据库索引开销；实际数据盘容量、每人/全局累计配额和长期保留策略仍需另行运维配置。

检索当前采用 PostgreSQL 有权限范围内的词法/字面匹配，支持中文子串及英文词项。
**没有把已安装的 Milvus/Elasticsearch 假装成已完成的语义检索**；未来检索后端须维持同样的当前权限和版本检查。

## 固定插件执行器

发行代码中的注册表允许：

- `knowledge-qa@1.0.0`：真实调用员工的个人模型代理，使用明确选定的文档版本/文本附件做问答；模型仍由个人可用目录复核。
- `synthetic-summary@1.0.0`：确定性文本统计，只在 `HYBRID_TEST_FIXTURES=true` 时可运行，明确标记 simulation。

管理员安装插件和分别授权。固定版本的 runner/model policy 由数据库触发器禁止修改，变更应发布新版本并有相应发行注册表。
请求不能携带执行代码、可执行路径、任意网络地址或工具清单。知识问答不是完整远程 DSH 工具 Host。

任务按员工请求 ID 幂等；同一会话只允许一个运行轮次。默认全局/工作区/员工同时运行上限为 8/4/2，超限 429。
`CLOUD_TASK_GLOBAL_LIMIT/WORKSPACE_LIMIT/EMPLOYEE_LIMIT` 分别可配置为 1–64/1–32/1–8。
默认模型任务 120 秒超时，可用 `CLOUD_TASK_TIMEOUT_SECONDS` 在 5–180 秒内设置。
超时失败保留回执；同一请求重试不再次调模型。超过超时加 60 秒的残留 running 任务在下次准入时记为 interrupted。
这不是分布式持久任务队列、自动重试业务执行器或容量承诺。

## 管理员开通（服务器本机工具）

工具 `python -m app.workspace_admin` 只能在配置好的 Gateway 环境里由有 OS/数据库运维权限的管理员执行。
`--operator` 仅记录审计工号，不是拿任意工号就能远程授权的身份机制。无公网自授予接口；不传数据库密码参数。

```bash
python -m app.workspace_admin --operator ADMIN_NO create --kind department --organization IAM_DEPT_CODE --name '部门云工作台'
python -m app.workspace_admin --operator ADMIN_NO create --kind group --name '跨部门研发小组'
python -m app.workspace_admin --operator ADMIN_NO member --workspace WORKSPACE_UUID --user EMPLOYEE_NO --can-chat --can-retrieve --can-publish --can-share
python -m app.workspace_admin --operator ADMIN_NO plugin-install --workspace WORKSPACE_UUID --plugin knowledge-qa
python -m app.workspace_admin --operator ADMIN_NO plugin-member --workspace WORKSPACE_UUID --plugin knowledge-qa --version 1.0.0 --user EMPLOYEE_NO
python -m app.workspace_admin --operator ADMIN_NO member --workspace WORKSPACE_UUID --user EMPLOYEE_NO --no-enabled
python -m app.workspace_admin --operator ADMIN_NO document --workspace WORKSPACE_UUID --document DOCUMENT_UUID --no-enabled
```

`member` 是完整权限替换，未列出的能力默认关闭；撤销不会删除历史或其他成员数据。
管理员命令记录变更动作与操作者，不输出密钥或正文。真实环境的 IAM 目录、员工工号和准入策略仍需运维配置；本地测试不会自动授权公司员工。

## 验证范围

2026-09-18：已有企业范围回归 + 新版 PostgreSQL 临时独立 schema 测试共 29 项首次通过，
随后补充请求缓存身份过期/调部门两项回归通过；分页和原子存储额度有独立测试。
覆盖跨部门组、私有/共享检索、版本冲突与双写竞争、固定源混合问答、员工绑定、幂等、撤权后派生结果封锁、执行中撤权丢弃结果、超时和并发准入。
使用合成员工与受控模型回调；公司 IAM/真实 TokenKey、长沙部署、真实业务插件、负载测试不在这个结果内。
