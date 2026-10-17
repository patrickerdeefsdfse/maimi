> 2026-09-17：下文旧平台Cookie/query-user接入仅作历史记录。新接入以当前根目录1.docx（SHA256 3a77113615712c6a25de51f2bf99989164b29f0494dfd4a1bbdb3a469d979b5f）为准，见[IAM-DIRECT.md](IAM-DIRECT.md)。项目迁移仍在后续阶段。

# 测试服 SSO 接入

本页依据本地 `1.docx` 的测试环境说明整理。当前只允许测试地址，正式地址和真实 Token 不写入仓库。

## 浏览器行为

- 请求 Gateway 时必须携带 Cookie：Fetch 使用 `credentials: "include"`，Axios 使用 `withCredentials: true`。
- access_token 和 refresh_token 是 HttpOnly Cookie，前端不得读取或保存。
- 前端只读取普通 Cookie `token_expires_at`；临近过期时调用 refresh 接口。
- 刷新返回 401/403 时跳转 `https://platformai-test.megmeet.com`。
- 登出调用测试服 logout 接口，成功后跳转响应中的 `logout_url`，以清理 IAM 全局会话。

## 测试接口

| 用途 | 方法 | 地址 |
|---|---|---|
| 当前用户 | POST | `/v1/auth/query-user` |
| 刷新 | POST | `/v1/auth/refresh-token` |
| 登出 | POST | `/v1/oauth/logout` |
| 权限验证 | 按需调用 | `/v1/endpoint/verify-access` |
| 监控上报 | POST | `/v1/monitor/service-monitor` |

## 浏览器免控制台检查

新 Gateway 提供只读检查地址：

`GET /v1/system/sso-check`

在与测试服登录相同的域名下直接打开即可。成功时只返回
`{"status":"authenticated","authenticated":true}`，不会返回用户编号、组织或 Cookie。
如果 `AUTH_REQUIRED=false`，返回 `auth_disabled`；如果认证未通过，通常由中间件返回 `403`。

测试入口必须使用测试服实际登录域名 `platformai-test.megmeet.com`。Cookie 不会在
`platform-ai.megmeet.com` 与 `platformai-test.megmeet.com` 之间共享。

如果隔离测试通过本机 Nginx 代理访问自签名证书，可在测试环境临时设置
`AUTH_TLS_VERIFY=false`。正式环境必须保持 `true`，并使用受信任证书。

测试时可临时设置 `BROWSER_SMOKE_ENABLED=true`，然后打开
`/v1/system/browser-smoke`。页面只提供一次测试按钮，完成模型请求和历史读取；正式环境保持关闭。

## 后端原则

Gateway 从 Cookie 读取会话并向测试服查询用户，用户上下文只放在服务端请求状态中。企业项目路由必须再次检查组织、角色和插件能力；普通 DSH 对话不绕过统一认证，但不强行调用企业插件。
