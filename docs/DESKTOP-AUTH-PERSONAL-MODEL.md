# 桌面登录与个人模型代理

更新：2026-09-22。桌面交接是本应用协议；正式IAM和TokenKey首次登录/聊天已由公司试用验证。r13按用户最新确认改为登录时取Key、麦米最长24小时会话，随后再做公司验收。

## 登录边界

Electron 主进程生成实例 UUIDv4 和随机 verifier，仅内存保存；`POST /v1/desktop/auth/start` 提交实例与 SHA256 verifier 的 base64url challenge，返回一次性 flow、系统浏览器地址、300 秒期限和验证码。

系统浏览器完成IAM授权码流程。Gateway用表单正文换access_token，查询可信profile；公司sso_tokenkey模式取其username作为工号，再与TokenKey返回username精确核对。用户核对桌面验证码后确认；nonce和HttpOnly、SameSite Strict Cookie绑定该浏览器，未经确认不能兑换。

确认 HTML 单独使用 `Referrer-Policy: strict-origin`：真实浏览器 form POST 保留精确 Origin，Referer 不携带 callback code/state。其他认证响应保持 `no-referrer`。确认请求不接受缺失、null 或外域 Origin，仍要求浏览器绑定 Cookie、一次性 nonce 和桌面实例证明。

`POST /v1/desktop/auth/exchange` 提交 flow、同一实例与 verifier；未完成返回 202，成功后原子消费 flow 并返回短期应用 Bearer 和公开身份。错误实例或 verifier 不消费合法 flow；重复兑换失败。此 Bearer 不是 IAM access_token，浏览器确认页也不获得它。

应用Bearer每次最多900秒，不超过底层麦米会话。主进程在到期前以同一实例和原verifier请求 `/v1/desktop/auth/renew`；服务端校验本地会话和原子Redis grant，在24小时绝对截止时间内续期。公司新模式不因此重查IAM或调用TokenKey。verifier仅留主进程内存，云lease可保持原值，Host心跳不延长员工自身登录。

`GET /v1/desktop/auth/session` 查状态；`POST /v1/desktop/auth/logout` 撤销本端grant和底层麦米会话，不影响另一端独立会话。退出与续期并发不能复活授权；会话达到24小时期限后重新IAM登录。IAM短令牌过期不影响已经建立的新会话。没有IAM refresh grant或跨登录后台任务委托。

Bearer仅用于明确受保护路由。每次请求复验麦米会话和身份绑定，模型还须在本次登录取得的目录内；客户端不能覆盖工号或权限。公司使用正式IAM，回调为 `https://platform-ai.megmeet.com/v1/iam/callback`，详见 [IAM配置与注册](IAM-DIRECT.md)。

## 公司个人 TokenKey 契约

服务端固定配置的请求目标：

```http
POST https://tokenkey.megmeet.com/api/external/user-credentials
X-API-Key: <服务器独立secret文件内容>
X-IAM-Access-Token: <本次登录中仍有效的IAM access_token>
```

请求不带身份JSON、form或query。员工号不是客户端可填写参数；TokenKey根据本次IAM token返回凭据，Gateway验证username精确等于可信profile取得的工号，不能仅凭iam_user_name建立身份。可选directory兼容模式使用已验证目录映射。

要求的返回结构：

```json
{
  "ok": true,
  "username": "<可信工号>",
  "iam_user_name": "<显示名>",
  "api_key": "<该员工个人Key>",
  "models": [
    { "model_code": "glm-5.3-flash", "model_name": "GLM-5.3-Flash" }
  ]
}
```

`ok` 必须是布尔 true；个人 Key 必须非空且可安全放入 Header；模型目录逐项检查 code/name，不接受非法或重复项。有效 `models: []` 表示此员工未获模型许可，返回空目录，任何模型调用都被拒绝。缺失 models、错误形状、工号不匹配、上游失败或重定向不会触发共享 Key 回退。

用户2026-09-22确认：在IAM授权短期内取得的个人Key后续可持续使用。公司模式仅登录时调用上述接口，个人Key/模型目录在服务器按麦米会话加密保存，最长24小时；后续目录和模型请求读本地绑定记录，不重新调用TokenKey。重新登录会取得新的Key/目录。模型上游拒绝个人Key时返回重新授权的固定错误，不回显正文或凭据。TLS、响应大小和身份一致性检查继续保留。

## 登录凭据的保存与删除

新公司会话不持久化IAM access_token。使用独立 `IAM_TOKEN_ENCRYPTION_KEY_FILE` 将个人Key与模型目录加密后写入Redis，密文绑定版本、会话摘要、账号、工号和截止时间；读取时复验session、密文绑定及剩余TTL。

退出或麦米会话到期后不能再读取该Key；旧版sso会话需重新登录，密文/身份绑定不符则拒绝。OAuth client_secret、TokenKey X-API-Key和加密key均留服务器，个人Key不进入桌面EXE、renderer、浏览器或DSH设置文件。

## 个人目录和模型请求

`GET /v1/personal-model/models` 返回该员工的 OpenAI 风格目录：`data[{id,name,object,owned_by}]` 和 `default_model`。如果配置了 `PERSONAL_MODEL_ALLOWED_MODELS`，取它与个人目录的交集；没有配置时以公司返回的个人目录为准。DeepSeek V4.1 Flash 在有效目录内时作为默认偏好，否则取目录第一项；空目录的 default_model 为 null。默认偏好不赋予使用未授权模型的权限。

本机原生 DSH 经桌面主进程的内存代理调用 `/v1/personal-model/chat/completions`。renderer 和 DSH 设置文件都拿不到真实个人 Key；代理不调用个人历史存储，不把普通本地聊天正文写入 PostgreSQL。它会将必要请求正文送往所配置的模型服务，因此“历史本地保存”不表示模型请求本身不出电脑。

取 Key 前后复验身份，再检查本次模型属于员工有效目录；不接受正文覆盖工号、凭据或上游 URL。一次模型失败不自动重试。JSON 返回前再检查身份和租约，SSE 输出期间检查期限并每 5 秒复验会话；退出不能撤销已完成的模型计算。

云任务调用同一个 `cloud_chat_complete(TrustedActor, messages, model, request_id)`，同样受个人 Key 和模型许可约束。插件开发者选择模型也不能越过员工许可；部门及插件权限由云服务另行校验。没有可让浏览器填任意工号的代调用接口。

## 配置与本地验证

生产适配器默认 `PERSONAL_KEY_ADAPTER=tokenkey`，配置如下：

| 配置 | 用途 |
| --- | --- |
| `PERSONAL_KEY_TOKENKEY_URL` | 默认公司 `/api/external/user-credentials` HTTPS 地址，固定在服务器配置 |
| `PERSONAL_KEY_SERVICE_SECRET_FILE` | TokenKey X-API-Key 私密文件 |
| `IAM_TOKEN_ENCRYPTION_KEY_FILE` | 服务器独立 Fernet key 文件 |
| `PERSONAL_MODEL_BASE_URL` | 默认 `https://tokenkey.megmeet.com/v1` |
| `PERSONAL_MODEL_SIMULATION` | 默认 false；true 只允许 IAM_DEV_MODE=true 且模型上游为受限本地地址，误用到公司/生产目标会拒绝配置 |
| `PERSONAL_MODEL_ALLOWED_MODELS` | 可选管理员进一步限制的模型 code 逗号列表；不设置则使用个人目录 |
| `DESKTOP_APP_TOKEN_TTL_SECONDS` | 当前默认 900，范围 60 至 900 秒 |
| `MAIMI_SESSION_TTL_SECONDS` | 公司麦米会话默认86400秒，绝对到期，每天重新登录 |

缺配置、错误 adapter 名称或身份失败均关闭访问，不读取旧公共 `TOKENKEY_API_KEY` 作为替代。旧 `synthetic` adapter 仅保留开发兼容，不代表公司接口。

本机 Compose 现也使用 `tokenkey` adapter，但 URL 指向 `http://mock-company:8081/api/external/user-credentials`，独立 secret 与 IAM token 全部是合成值；`PERSONAL_MODEL_BASE_URL` 指向模拟模型服务，并显式设置 `PERSONAL_MODEL_SIMULATION=true`，云模型结果保持 simulation=true。只在 `IAM_DEV_MODE=true` 且目标为 localhost、127.0.0.1 或 mock-company 时允许开发 HTTP。使用真实契约的 adapter 不表示请求已经访问真实公司服务，也不能根据 adapter 类名推断模型是否为模拟服务。

2026-09-18 定向测试：IAM、桌面认证、个人代理三组 **83 项通过**；空模型目录调整后代理 **35 项重跑通过**；随后补充显式 simulation 标志的 5 项校验，个人代理最终 **40 项通过**。包括 form/无 query/13 位时间戳、加密 session 绑定和期限、旧 session 重登、双 Header 空 POST、错工号、非法目录、重定向、TLS 降级、普通及云模型许可、退出、错误脱敏及模拟标识边界。均为合成验证，不代表真实公司注册、Key、额度或部署验收。

历史 `desktop_auth_smoke.py` 和 `fixture-browser.cjs` 手工设置 Origin，不能验证浏览器原生 form 行为。`desktop-local/scripts/browser-login-smoke.cjs` 独立使用真实 Chromium form 提交，已用于修复前复现及修复后 A/B 验证；保留它与 HTTP 探针两种覆盖。真实公司登录及正式桌面发行仍须在注册参数到位后另行验收。
