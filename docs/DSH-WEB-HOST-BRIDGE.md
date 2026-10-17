# DSH Web 原生 Host 桥接方案

更新：2026-09-17。本文件保留服务器原生 Host 桥接边界。服务器两账号个人 Host/SSO 试用已有人工作用验收；本机桌面当前使用独立原生 DSH 容器和合成身份/模型。两者不可混同，具体状态以 `codex-context` 的最新记录为准。

旧自写 React 页面和其发布脚本已退役，不能用它代替原生 DSH。当前本机构建入口见 [`dev-local/dsh`](../dev-local/dsh/README.md)，桌面壳见 [`desktop-local`](../desktop-local/README.md)。

## 目标

让员工使用 DSH 原生 Web 界面，同时复用 Megmeet 的 SSO、项目路由、插件权限、统一历史和模型目录。浏览器不接触 API key、DSH_HOME 或企业服务凭据。

## 为什么不能只发布 dist

上游 DSH 源码的 `apps/web` 与 `AppWebEntry` 是其浏览器启动壳，并非本仓库已退役的 React 原型目录。DSH Web 的聊天、插件和会话能力由 Host 注入；下列名称是服务器 0.1.5rc2 契约记录，新版本需重新核验：

- `dsh-host-webserver` 提供 HTTP/WebSocket 载体和 fallback。
- `dsh-client-modules` 提供 `/plugins` 组合和启动 manifest。
- `dsh-client-connection` 提供认证与 `/api` Remote carrier；完整 profile 的 `api-gateway` 注册精确 `/api/remote.mux` WebSocket。
- Host 使用独立的 host-only browser-session Cookie；它不能作为所有员工共享的身份凭据。

因此，静态文件只能作为 Host 的 fallback，不能替代 Host。

## 目标拓扑

```text
Browser
  │  Megmeet SSO Cookie + Host session Cookie
  ▼
Nginx /megmeet-ai/
  ├─ static + DSH Web fallback ─────► per-user dsh-web-host
  ├─ /api/remote.mux ───────────────► same user's Host bridge
  └─ /v1/* ─────────────────────────► Gateway
                                      ├─ SSO and tenant ACL
                                      ├─ DSH Adapter/runtime
                                      ├─ project/plugin routing
                                      └─ PostgreSQL history and audit
```

## 原服务器桥接实施阶段（历史说明）

1. **契约采集**：在 Linux 只读提取已安装 DSH 包的入口、类型声明、profile roster、manifest 和 remote.mux 路径；不安装、不启动、不请求模型。
2. **原生 Host 验收**：复用已安装 runtime wheel 的完整 `web` profile 和资源。先执行断网临时容器探针，之后验证浏览器真正渲染。正式部署再配置受控入口与每用户/会话独立状态，避免手工重写 Host。
3. **身份桥接**：Host session 与 Megmeet SSO 通过服务端映射，浏览器只携带 HttpOnly Cookie；Gateway 仍负责组织、项目和插件 ACL。
4. **能力桥接**：普通对话使用原生 Web Host 自带 Agent；从模型服务、企业工具和会话事件层桥接平台。现有 Python Adapter 作为 REST 回归入口，同一轮消息只执行一次 Agent。企业项目经服务端权限检查后使用插件/工具调用，统一历史及审计需要独立适配与验收。
5. **验证切换**：在独立测试路径验证 DSH 原生页面、登录、普通对话和历史；企业项目/插件路由另行实施。旧 React 验证页现已从本地活动工程删除。

首轮 Host 验证建议使用独立域名或独立 Nginx server block 的根路径。DSH 默认注册根路径、`/api`、精确 `/api/remote.mux` 和 `/plugins/*`；直接挂在 `/megmeet-ai-r10/` 子路径前，必须先验证 prefix strip、WebSocket Upgrade、根路径 token 交换和 `trustedHosts`，否则容易出现资源路径或 403 问题。

## 安全边界

- Host 不监听公网地址；外部访问只经过 Nginx TLS。
- Host 的 `trustedHosts` 必须包含真实反代 authority；Host/Origin 校验与公司 SSO 是两层独立检查。
- 每个 Host RPC/stream 都需要该 Host 自己的 host-only Cookie；不能让全体员工共享一个 Host 进程和 Cookie。正式方案按用户或会话建立 Host/DSH_HOME 隔离。
- 不共享 Host-only browser-session Cookie。
- API key、DSH_HOME、模型上游地址只存在 Gateway/Host 服务端。
- 企业项目的 organization、project 和 plugin 权限以服务端 SSO 上下文为准。
- 原生页面与 Gateway REST 分别验证，不再保留自写 React 页面作为活动回归入口。

## 当前接续

- 保留 `services/native-entry` 的身份、个人 Host 与续期适配，按新版本契约逐项联调；本机桌面尚未接真实公司 SSO/员工 Key。
- 原生企业插件选择、任务执行与平台历史关联需要独立实现，不重建聊天界面。
- [`断网原生 Web 探针`](../ops/DSH-NATIVE-WEB-PROBE.md) 属于服务器 rc2 历史验证入口，不是当前本机新版的升级脚本。

服务器 rc2 packaged runtime 包含 `web` profile 与前端资源这一点，同时由历史 runtime README 和 [rc2 官方 README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/dsh-v0.1.5-rc.2/python/sdk-runtime/README.md) 支持。该历史依赖形态不要求当前本机原生容器重新采用 Python wheel 打包链路。
