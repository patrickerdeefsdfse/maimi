# IAM 直接对接：当前实现与公司注册

更新：2026-09-22。公司r12已部署；r13按用户确认的“单点登录取Key，麦米每天重新登录”协议实施，具体公司应用结果以交付记录为准。

## 接口依据与变更

组织目录、账号映射、授权入口及 profile 继续依据本轮新版 `1.docx`（SHA256 `3a77113615712c6a25de51f2bf99989164b29f0494dfd4a1bbdb3a469d979b5f`）。用户 2026-09-18 明确补充的 token 交换及个人 TokenKey 契约优先：**accessToken 请求改用表单正文，替代 Word 示例中的 query 传参**。旧门户 `query-user`、`refresh-token` 不能补充或替代这一直接 IAM 协议。

| 步骤 | 当前实现 |
| --- | --- |
| 授权 | 服务端生成 `/esc-sso/oauth2.0/authorize` 跳转，带 client_id、response_type=code、注册 redirect_uri、随机 state、毫秒时间戳 |
| 回调 | 固定 `/v1/iam/callback`；校验浏览器绑定 Cookie 和一次性 state 后交换 code，超时也不重放 code |
| 换 Token | `POST /esc-sso/oauth2.0/accessToken`，`Content-Type: application/x-www-form-urlencoded`；六个字段全部在正文，URL 不携带 client_secret 或 code |
| Token 返回 | bearer access_token 和 expires_in 仅用于本次登录与获取TokenKey；refresh_token 不保存、不使用 |
| 查账号 | `GET /esc-sso/oauth2.0/profile?access_token=...`，只取 `attributes.username`，不回退 profile 顶层 id |
| 查工号 | 公司r10 `sso_tokenkey`：可信profile.username精确匹配同一IAM token取得的TokenKey username；无目录依赖，无部门声明。可选`directory`模式仍按目录映射，默认兼容旧部署 |
| 个人凭据 | 登录期间用仍有效的IAM access_token请求TokenKey，核对username等于可信工号；个人Key及模型目录随后由服务器按麦米会话加密保存。详见[个人模型协议](DESKTOP-AUTH-PERSONAL-MODEL.md) |

Token 表单字段：

```text
grant_type=authorization_code
oauth_timestamp=<13位毫秒时间戳>
client_id=<此环境注册的client_id>
client_secret=<此环境服务端secret文件内容>
code=<本次一次性授权码>
redirect_uri=<注册的准确回调URL>
```

`redirect_uri` 在代码中保留原始 URL，由 HTTP 客户端执行一次表单编码；不能先手动编码再交给客户端。TLS 验证开启，不跟随 token/profile 重定向，不继承环境代理，不以失败响应正文作为公开错误信息。

## 注册目标：长沙服务端接收回调

给 IAM 管理员登记的回调规划为：

```text
https://platform-ai.megmeet.com/v1/iam/callback
```

2026-09-21 用户明确选择直接接入 **IAM 正式服** `https://iam.megmeet.com`。授权入口与后台 API 同时使用此地址，应用 client_id、client_secret 和注册回调必须属于正式环境。公司配置向导拒绝测试地址；从测试配置切换时不自动复用旧应用 ID 或 secret，数据库密码和会话加密密钥仍保留。

用户已有 OAuth clientID/clientSecret，没有目录密钥；正式服对应应用凭据的实际配置留在服务器私下完成。文档第3节数据同步与第2节单点登录是不同接入方式，目录同步不是SSO协议的前置条件。当前公司部署向导只询问IAM地址、clientID、clientSecret、TokenKey服务X-API-Key。真实正式服IAM账号与TokenKey工号匹配、正式token是否被TokenKey接受仍待公司联调。过去测试域名的 DNS/TLS 结果不能替代正式域名验证。

桌面运行在员工电脑；系统浏览器把 IAM 回调送到长沙 Gateway，Gateway 完成换 token、查 profile 和工号映射。桌面凭应用自己的实例证明兑换短期凭据，不需要在员工电脑开一个接收公司 IAM 回调的公共服务。

## 服务端会话与秘密

应用Cookie和桌面Bearer均是不透明应用凭据。公司sso_tokenkey新会话在Redis保存账号、工号、版本、固定截止时间与Fernet加密的个人Key/模型目录。密文绑定sid摘要、账号、工号、版本和截止时间，不可跨会话复制。沿用独立 `IAM_TOKEN_ENCRYPTION_KEY_FILE` 服务器密钥；不持久化IAM access_token，也不把个人Key明文写入Redis AOF。

2026-09-22用户明确IAM只负责登录，个人TokenKey可在取得后持续使用，并选择每天重新登录。新麦米会话默认最长86400秒，绝对到期不滑动延长。登录时仍要求IAM凭据有效直至身份和Key绑定完成；完成后不再以IAM短期限限制麦米会话，也不在日常请求调用IAM/profile或TokenKey。桌面grant每次最多900秒，可在这份麦米会话的剩余期限内续期；云lease和模型输出同样受本地截止时间与退出约束。目录模式保留原兼容语义。

退出删除对应应用会话及相关桌面grant；到期、身份/密文绑定损坏或加密密钥不匹配时拒绝访问。旧sso_tokenkey会话需要重新登录建立版本2记录，不自动延长旧期限。个人Key或模型许可变化在重新登录获取的新目录和实际模型上游的校验中体现；新协议不承诺实时同步IAM注销/停用。未实现IAM refresh grant，也不跨越麦米会话期限委托后台任务。

公开身份、Cookie、桌面交接响应不包含 IAM token、OAuth client_secret、TokenKey 服务 secret 或个人 api_key。OAuth、目录、TokenKey 服务 secret 和会话加密 key 用途不同，均留在服务器；不打入 EXE、网页 JavaScript、桌面配置或 renderer 存储。

## 配置项

| 配置 | 来源及用途 |
| --- | --- |
| `AUTH_MODE=iam_direct` | 应用选择直接 IAM 登录 |
| `IAM_IDENTITY_MODE=sso_tokenkey` | 公司r10使用；账号与TokenKey工号必须精确相等，无部门权限推断。省略时兼容旧directory模式 |
| `IAM_BASE_URL` | 选定环境 IAM origin；服务端换 token 和查 profile |
| `IAM_AUTHORIZATION_BASE_URL` | 浏览器可访问的同环境 IAM origin，通常同 IAM_BASE_URL |
| `IAM_CLIENT_ID` | IAM 为此环境注册的应用 ID |
| `IAM_CLIENT_SECRET_FILE` | OAuth secret 所在服务器私密文件 |
| `IAM_PUBLIC_ORIGIN` | 公司规划 `https://platform-ai.megmeet.com` |
| `IAM_REDIRECT_URI` | 已登记的准确 URL；公司规划为上述固定回调 |
| `IAM_POST_LOGIN_PATH` | 普通网页登录完成后的固定本应用路径 |
| `IAM_LOGOUT_RETURN_URI` | 与 public origin 同源的固定退出返回地址 |
| `IAM_TOKEN_ENCRYPTION_KEY_FILE` | 独立 Fernet key 文件，用 `Fernet.generate_key()` 生成，不与其他 secret 共用 |
| `MAIMI_SESSION_TTL_SECONDS` | 公司新会话绝对期限，默认86400秒，每天重新登录；生产r13明确设置86400 |
| `REDIS_URL` | 私有 state、会话、桌面 grant 存储 |
| `IAM_DIRECTORY_ORG_URL` / `IAM_DIRECTORY_ACCOUNT_URL` | 仅可选directory模式使用的同环境组织和账号接口 |
| `IAM_DIRECTORY_API_KEY` / `IAM_DIRECTORY_API_SECRET_FILE` | 目录同步自己的 issuer/secret，不推断等同 OAuth 凭据 |
| `IAM_DIRECTORY_SNAPSHOT_FILE` | 目录 worker 与 Gateway 共享的私有快照 |

环境模板和部署 overlay 只是准备材料，不证明服务器已配置。生产需要可用 HTTPS 证书链、私有 Redis、正确文件读取权限和与登记值一致的回调。`IAM_DEV_MODE=true` 与允许本地 HTTP 的配置只用于合成环境，不应复制到公司生产配置。

Token 交换正文不落日志；profile URL 仍含 access_token，callback URL 含一次性 code/state。应用过滤相关 HTTPX/访问日志，正式代理和 APM 也须避免记录这些秘密。Fernet 加密保护 Redis 持久化，不能代替服务进程权限、secret 文件权限和日志边界。

## 桌面已实现的交接与验证边界

桌面主进程生成实例和 verifier；服务端创建一次性登录 flow。系统浏览器完成 IAM 登录后显示确认码，用户确认后桌面凭实例和 verifier 兑换短期应用 Bearer。精确 Origin、HttpOnly 绑定 Cookie、一次性 nonce 和实例证明均已实现。确认页单独使用 `strict-origin` 解决真实浏览器 form POST 的来源问题，其他认证响应保持 `no-referrer`。

这是应用自己的桌面交接，不表示 IAM 支持原生 PKCE/public-client/桌面自定义回调。不能把 IAM access_token 或 client_secret 放入桌面 URL 代替交接，也不能假定系统浏览器与 Electron 共享 Cookie。

2026-09-18 定向验证：IAM、桌面认证、个人模型三组 **83 项通过**；有效空模型目录调整后，个人代理 **35 项重跑通过**；随后补充显式 simulation 标志边界，个人代理最终 **40 项通过**。验证包含严格表单、加密会话、过期/退出/篡改、工号绑定、个人模型目录、无越权模型和无共享 Key 回退，均使用合成数据。这些结果不证明公司真实接口、注册、长沙部署或全员容量已验收。

目录默认全量同步间隔和快照时限仍是应用策略；大目录吞吐、真实撤权时限、正式软件签名/自动更新、公司环境端到端登录与模型计费仍需分别验证。
