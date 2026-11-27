# 钉钉私聊本人授权 · QA 接线

正式 r105 网关的个人模型代理依赖一份实际员工 IAM 会话。钉钉事件的 senderStaffId 只用于识别已配对的发送人。QA 分支从**员工本人已登录的同源浏览器**调用现有 require_trusted_actor、issue_lease 和 lease_actor，再用现有 cloud_chat_complete 完成一条纯文字提问。模型调用前后会核对该员工的当前 TokenKey 身份与模型权限。

默认关闭。开启前在正确的隔离数据库部署 identity-schema.sql 与 delegation-schema.sql，向专用受限进程提供一把独立 32 字节随机密钥文件 `MAIMI_IM_CONSENT_KEY_FILE`，配置 `MAIMI_IM_MODEL` 为现有公司模型白名单允许的模型，设置 `MAIMI_IM_MODEL_DELEGATION_ENABLED=true`。默认的资料库、机器人入站 HMAC、身份绑定与去重配置见《现有钉钉机器人接入》。专用服务须位于网关信任边界，有受限的原 IAM 会话存储、公司模型授权配置和原网关依赖。EIP 与机器人进程均不能接收个人模型密钥、公司浏览器 Cookie 或云端租约。不要为此开放通用 IAM、Redis、TokenKey 接口。

## 员工操作

1. 已登录的员工从公司麦米域名打开 `/maimi/im/dingtalk/` 并点击“生成配对指令”。页面返回一条有效五分钟的指令。员工把指令发到同一机器人的**私聊**，将机器人返回的确认码填回自己原浏览器。配对记录自签发日起最多保留 30 天，可撤销。
2. 员工明确点击“授权本次文字问答”后才允许从这次钉钉私聊调用模型。该授权最多 15 分钟，截止时间还受员工原 IAM 会话限制。浏览器只显示到期时间。服务器把原有租约用 AES-GCM 封存在 PG，附带数据约束机器人安装、员工、绑定代次、用途和期限。两台 AP 的应用密钥须一致；不能使用钉钉 AppSecret。过期授权由后台每五分钟清理，可用范围在有效期结束时即截止。
3. 员工发“麦米：用一句话解释……”后，由该员工自己的模型权限处理。这一版仅支持单条纯文本问答，不调用文件、工具或他人工作区。重复事件沿用原处理编号；结果未确认时不自动重放。员工退出原 IAM、授权到期或撤销后，下一次问答要求重新授权。

## 路由边界

| 路由 | 谁能访问 | 作用 |
| --- | --- | --- |
| `GET /maimi/im/dingtalk/` 及两个同目录静态文件 | 公司浏览器 | 只提供配对界面 |
| `POST /v1/im/dingtalk/pair/start` | 同源 IAM Cookie | 以当前员工身份产生指令，清除该员工上一次未完成申请 |
| `POST /v1/im/dingtalk/pair/confirm` | 同源 IAM Cookie | 提交同一票据和私聊确认码；跨员工确认拒绝 |
| `POST /v1/im/dingtalk/consent` | 同源 IAM Cookie | 请求体 scope 固定为 dingtalk-private-text-v1 |
| `POST /v1/im/dingtalk/revoke` | 同源 IAM Cookie | 解除本人配对与委派 |
| `POST /internal/maimi-im/v1/dingtalk` | 内网 EIP 转发 | 强制 HMAC 与时效，核对机器人、企业、员工、绑定与重复投递 |

浏览器路由只能反代到允许的公司域名；原 `/internal` 入口仅可由 EIP 经过受限网络和 HTTPS 访问。有效期内每次调用依然通过原网关 lease_actor 及 PersonalModelBroker 的身份和权限复核，不造服务工号、不读员工聊天历史、不把通用 Bearer 交给 EIP。

## 验证与待办

已在不通外网的 r105 镜像和独立 PostgreSQL 16 运行完整 QA。两名合成员工分别配对；验证跨员工抢票失败、未授权不调模型、两人调用携带各自不同的模型凭据、同一事件只产生一次调用，以及退出、过期、解绑、错绑和跨站请求拒绝。真实执行了 r105 的浏览器验身份、issue_lease、lease_actor、cloud_chat_complete 与个人模型代理。IAM 会话记录、Redis、凭据服务和模型上游为合成实现，未使用真实员工账号。记录见 delegation-validation.json。

尚需确认现场原回调方式、部署私密凭据与同源入口、实测员工配对和真实模型。现有 MagicAPI 分流片段读取超时约 5.5 秒，模型可能更久。现已采用快速收件、持久任务和本人主动取件，具体见《QA回调自检与慢问答》。现场仍需验证原平台实际收件和取结果。因此目前只可标记 QA 验证通过。
