# 员工连接器：钉钉与联网搜索

2026-09-21 接续核验。本地源码实现协议 `megmeet-connectors-v1`，不依赖 DSH 包或插件生命周期。真实公司钉钉授权、Brave 服务凭据、公司网络均尚未联调。此文不表示已部署，也不要求立即配置凭据。

## 产品行为

- 所有已通过 IAM 认证的员工都可看到两个连接器。未配置、未开通或未授权会明确显示相应状态，不伪装成功。
- 用户将连接器附加到当前原生会话后，DSH 薄工具适配层只调用该连接器公开的工具。模型目录、个人 TokenKey 和连接器凭据相互独立。
- 联网搜索只发送工具调用中的显式关键词，不自动上传整段对话、附件、员工工号或本机文件。公开网页读取不携带 IAM、钉钉、浏览器 Cookie 或员工 TokenKey。
- 钉钉读取限本人日历；可拟定本人日程、个人待办、发送给明确收件人的机器人文本消息。消息以公司应用机器人身份发送，不冒充员工本人。
- 创建日程/待办/发送消息均先返回内容预览，用户点击确认才调用写接口。确认接口不是模型工具，也不允许云工作进程的模型租约直接调用。
- 接收人和日程参与人限服务器账号映射中的该员工允许列表。默认额外允许自己；不开放整个企业通讯录浏览、修改组织或任意员工数据读取。

## HTTP 接口

`GET /v1/connectors/catalog` 返回 `protocol` 和 `items`。每项具有 `plugin_key`、`version`、`name`、`description`、`status`、`reason`、`tools`。每个工具含 `name`、`description`、`input_schema`、`available` 和 `requires_confirmation`。

状态为 `ready`、`limited`、`authorization_required`、`unavailable`。没有 Brave key 时联网搜索仍可选择，只有公开网页读取可用；`web_search` 不可用。钉钉未授权时显示连接按钮，其 `authorization_url` 是本系统的 `/v1/connectors/dingtalk/connect` 地址，没有凭据。

`POST /v1/connectors/invoke`：

```json
{
  "connector": "web-search",
  "action": "web_search",
  "arguments": {"query": "公开技术文档", "count": 5},
  "request_id": "b7c0d169-8788-46c0-bda2-f9f778df5500"
}
```

只读响应为 `status: completed` 和 `result`。写操作响应为 `status: requires_confirmation`、`confirmation_id`、`expires_at`、`preview`。前端必须以文本方式渲染预览，包括收件人的工号、姓名和完整待发内容；不能把上游文字作为 HTML。

`POST /v1/connectors/confirm` 接收 `{confirmation_id, confirmed:true}`，仅由真实用户确认交互调用；不能注册为 DSH 工具，不能在模型请求后自动调用。请求必须带有效桌面授权或浏览器 IAM Cookie 和同源 Origin。确认只有五分钟、同一员工、同一授权代次、同一参数及收件人映射有效，只执行一次。调用超时后的结果不确定会明确要求先检查钉钉，不自动重发。

工具一览：

| 连接器 | 工具 | 参数 | 行为 |
|---|---|---|---|
| dingtalk | calendar_list | start/end 含时区 ISO 时间、count≤50 | 本人主日历，最多31天 |
| dingtalk | calendar_create | start/end/title/description/attendees员工工号 | 生成预览，确认后创建 |
| dingtalk | todo_create | title/description/due_time_ms | 生成预览，确认后创建个人待办 |
| dingtalk | message_recipients | 无 | 只返回当前员工获准选择的收件人 |
| dingtalk | message_send | recipients员工工号/text | 生成预览，确认后机器人发消息 |
| web-search | web_search | query≤400字符/count≤10/freshness可选 | Brave公开搜索，返回标题摘要来源链接 |
| web-search | web_read | url | 公开HTTPS网页正文，最大512KiB响应/16000字符输出 |

公共路由复用 `enterprise_tasks._actor` 的可信员工与 Cookie Origin 检查。内部云工作进程通过 `personal_cloud.lease_actor` 保护的 catalog/invoke 固定别名调用；confirm 另要求由同源用户界面请求产生的30秒一次性证明，同时绑定该客户端租约与具体确认ID。模型仅有租约不能自行生成此证明。OAuth、管理映射和秘密读取不向模型工具开放。桌面与网页按原生 session 及客户端身份绑定所选插件，未选择的工具不能执行，模型工具不会获得确认票据。

## 钉钉企业接入配置

钉钉应用的 client ID/secret 与 IAM OAuth 的凭据是不同应用，不可混用。未配置钉钉不影响 IAM 登录或常规聊天。

| 配置 | 用途 |
|---|---|
| DINGTALK_CLIENT_ID | 钉钉企业应用Client ID |
| DINGTALK_CLIENT_SECRET_FILE | 钉钉Client Secret的服务器只读秘密文件 |
| DINGTALK_CORP_ID | 公司钉钉组织ID，OAuth返回必须一致 |
| DINGTALK_ACCOUNT_MAP_FILE | 经管理员核实的工号与钉钉身份/收件人权限映射 |
| DINGTALK_ROBOT_CODE | 机器人Code；空值时不允许发送消息，其余能力独立 |
| DINGTALK_OAUTH_SCOPES | 已在钉钉后台开通的委托权限，必须包括openid、corpid |
| IAM_PUBLIC_ORIGIN | 当前系统对外HTTPS来源；钉钉使用自己的独立回调路径 |

默认委托权限字符串为 `openid corpid Contact.User.Read Calendar.Event.Read Calendar.Event.Write Todo.Todo.Write`。管理员需要在钉钉开发后台确认这些个人权限以及所用API的可用性；机器人发送是应用权限，也须单独开通。代码不会伪造权限开通成功。

注册地址为 `https://<系统域名>/v1/connectors/dingtalk/callback`。不能替换已有 IAM 的 `/v1/iam/callback`。

员工点击连接后，系统浏览器访问 `GET /v1/connectors/dingtalk/connect`。已有 IAM 浏览器登录会话必须与桌面员工一致；若系统浏览器尚未登录，应由入口先完成 IAM 登录再回到此固定路径。`POST /dingtalk/authorize` 可供同源网页弹窗流程使用。OAuth采用随机state和HttpOnly浏览器绑定Cookie，回调只接收文档中的 `authCode` 和 `state`，然后验证当前IAM员工、选中公司corpId和钉钉users/me的unionId。

映射样例（只是结构示例，不是真实账号）：

```json
{
  "version": 1,
  "accounts": {
    "M00000001": {
      "enabled": true,
      "union_id": "DINGTALK_UNION_ID",
      "user_id": "DINGTALK_STAFF_USER_ID",
      "display_name": "员工甲",
      "allowed_recipients": ["M00000002"]
    },
    "M00000002": {
      "enabled": true,
      "union_id": "SECOND_UNION_ID",
      "user_id": "SECOND_STAFF_USER_ID",
      "display_name": "员工乙",
      "allowed_recipients": []
    }
  }
}
```

工号取已有可信IAM身份，不接受前端传入员工所有者。钉钉unionId和userId不是同一字段：个人身份和日程参与人使用unionId；机器人发送使用userId。映射文件为服务器管理员提供的独立授权资料，不从姓名相似或员工自行输入的ID推断。映射缺失的员工仍能看到插件和开通提示，但不能访问钉钉。允许列表变更/禁用账号会使已有授权或确认失效。不能把IAM只有clientID/secret的问题重新变成登录前置目录要求；此映射仅约束可选钉钉功能。

用户令牌和刷新令牌只保存在Redis中的加密记录，复用服务器 `IAM_TOKEN_ENCRYPTION_KEY_FILE` 密钥；最大存活窗口7天，成功刷新可续期，刷新失败不绕过授权。Redis `GETDEL` 和CAS Lua防止确认重放及注销后刷新复活。`POST /dingtalk/disconnect` 删除当前员工令牌；已产生的确认随授权删除失效。任何授权码、令牌、应用secret不得进入前端、原生模型上下文、审计正文或HTTP访问日志。

创建日程必须返回有效 `id`，创建待办必须返回有效 `taskId`；空响应不能显示创建成功。机器人返回 `filteredStaffIdList`、`invalidStaffIdList` 或 `flowControlledStaffIdList` 时，返回部分接受或拒绝，并要求先查钉钉再重试；正常接受也仅表示上游接受请求，不表示收件人已经收到或阅读。确认后的异常响应与网络超时仍按结果不确定处理，不复用原票据重发。

## 下一版的可选部署入口

连接器开通采用独立的 `.connectors.env`。不要将 `connectors.env.example` 合并进 IAM 的 `.env`：IAM 配置器保留自己的字段白名单。默认 `start-server.sh` 不加载连接器挂载，不要求钉钉或搜索凭据，也不会受当前终端中同名连接器变量影响。

下一版完成新镜像和发行校验后，可使用同目录的 `connectors.env.example` 作为 `.connectors.env` 模板，填写独立应用标识与目录位置。`CONNECTOR_CONFIG_SOURCE` 指向现有服务器目录，以只读方式挂到 `/run/connectors`；目录须可由 Gateway 的 UID 10001 访问。启用钉钉时，目录中须有 `dingtalk-client-secret`、`dingtalk-accounts.json`；启用 Brave 时须有 `brave-search-api-key`，并将 `BRAVE_SEARCH_API_KEY_FILE` 设为 `/run/connectors/brave-search-api-key`。这三份文件不打入镜像、安装器或发行 ZIP；密钥内容不写进 `.connectors.env`。

源码提供两个明确入口（本轮未在公司执行）：

```bash
# 仅检查可选配置和文件元数据；不读密钥正文、不调用外部服务。
python3 check-connectors.py
# 未来已验证发行包的可选启动方式；可与 --nginx-network / --allow-running 组合。
bash start-server.sh --connectors
```

`--connectors` 才会增加 `.connectors.env` 与 `connectors.compose.yaml`，先通过独立字段/路径/必需文件检查和不输出展开值的 Compose 检查，再执行既有镜像校验与启动流程。检查失败不会继续启动；只通过文件元数据检查不代表密钥有效或用户已经授权。停用此可选配置时应保留秘密文件和账号映射，后续按已验证更新流程重新创建 Gateway。

当前公司 r9/r10 镜像没有本轮连接器代码；不能把这个选项或新源码复制到旧发行目录后混用旧镜像。尚未生成包含本轮改动的新发布包，上面的启动入口只对未来完成源码、镜像和清单校验的新版本生效。

## 搜索与公开网页边界

设置 `BRAVE_SEARCH_API_KEY_FILE` 指向服务器只读秘密文件，调用固定 `https://api.search.brave.com/res/v1/web/search`。没有密钥时只启用不需要密钥的 `web_read`，不返回伪造搜索结果。

公开网页只允许HTTPS/443，无用户名密码、片段或非HTTP协议；所有DNS答案必须为公网地址，实际连接固定已校验IP，TLS仍用原域名验证，防止二次解析绕过。重定向逐跳重新校验。请求不用系统代理，也不发送公司或浏览器身份。只读取HTML/纯文本，拒绝压缩载荷、超大响应和内部/回环/链路本地地址。响应正文标记为外部不可信内容。每员工每分钟30次、服务整体每分钟300次请求上限，不表示已通过公司容量验收。

公开网页读取运行在每 Gateway 进程最多4个独立子进程中；子进程使用隔离 Python 启动与绝对脚本路径，25秒超时或任务取消后终止并回收，包括卡住的 DNS/TLS。等待读取名额超过1秒会返回忙碌。钉钉与 Brave API 使用固定 HTTPS 地址、无环境代理及20秒请求总时限；真实公司出站访问仍待验证，个人 TokenKey 不提供搜索服务密钥或网络出口。

## 上游依据与验证范围

- 钉钉官方 MCP公开的能力与权限：[open-dingtalk/dingtalk-mcp](https://github.com/open-dingtalk/dingtalk-mcp)
- 钉钉官方委托OAuth流程与corpId说明：[浏览器获取用户委托凭证](https://open-dingtalk.github.io/developerpedia/docs/develop/permission/token/browser/get_user_app_token_browser/)
- 官方生成SDK的calendar_1_0、todo_1_0、oauth2_1_0、contact_1_0、robot_1_0：[aliyun/dingtalk-sdk](https://github.com/aliyun/dingtalk-sdk/tree/master/dingtalk/csharp/core)
- 日程字段说明：[钉钉日程资源](https://open.dingtalk.com/document/orgapp/event-resource)
- Brave官方搜索连接器及REST方案：[Brave Search MCP](https://github.com/brave/brave-search-mcp-server)

本地测试使用合成账号、内存/临时Redis和MockTransport验证请求形状、范围限制、加密、错误处理与一次性确认，不发送真实钉钉消息、不访问公司IAM、不开通任何公司应用权限。它证明本地实现与边界，不替代公司钉钉API联调。没有新发布ZIP，已有r9/r10制品未改。

本轮连接器与隔离 Redis 回归54项通过，包含并发一次消费、刷新CAS、上游空响应和部分拒绝、读取子进程取消回收。可选部署入口另有合成文件/命令替身测试及真实 Compose 解析测试，证据见 `output/connectors-20260920/resume-connectors-report.md`；这些检查未读取公司秘密、未启动公司服务。
