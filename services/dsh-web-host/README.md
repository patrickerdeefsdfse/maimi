# DSH Web Host bridge（骨架）

此目录记录 DSH `0.1.5-rc.2` 原生 Web Host 的部署边界。现有 Python runtime wheel 已包含完整 `web` profile 与前端资源，下一步直接使用其 `dsh` 命令；无需重新编写 Host 或拼装几项底层服务。**生产身份与数据桥接仍未实现，Linux 原生 Web 启动验收待用户回传结果。**

已准备 [`ops/run-dsh-native-web-probe.sh`](../../ops/run-dsh-native-web-probe.sh) 与配套 Python 探针：使用已验收 Gateway 的不可变镜像 ID，在独立断网临时容器中验证原生页面、模块资源、Cookie 认证及 WebSocket Upgrade，详见 [`原生 Web 探针说明`](../../ops/DSH-NATIVE-WEB-PROBE.md)。

## 目标

为每个已认证的 Megmeet 用户（或短期会话）提供一个独立的 DSH Host/Webserver。Host 负责 DSH 原生页面和连接协议，Gateway 继续负责公司 SSO、模型路由、项目权限、历史和审计。

浏览器只携带两类 HttpOnly Cookie：公司 SSO Cookie，以及由 DSH Host 自己签发的 host-only 会话 Cookie。两者不能合并，也不能在用户之间共享。

## 已确认的 rc2 连接面

- 静态页面和启动 manifest 由 DSH `frontend-static`/`client-modules` 组合提供。
- 普通 Remote 请求使用 `/api`。
- 流式连接必须使用精确路径 `/api/remote.mux` 的 WebSocket Upgrade。
- Host 必须校验 `Host`/`Origin`，`trustedHosts` 应包含真实反向代理 authority。
- 根路径的 `?token` 交换由 DSH Host 完成，换取 Host 自己的浏览器会话 Cookie。

这些条目来自 Linux 上 `dsh-v0.1.5-rc.2` 的只读契约采集；`connection` 负责认证和 `/api`，WebSocket mux 路由由完整 profile 中的 `api-gateway` 注册。不能只创建 WebServer、Connection 与静态服务就宣称拥有完整 DSH Agent。

## 配置骨架

复制 [`config.example.env`](./config.example.env) 为部署环境配置时，只填写测试或生产密钥引用，不填写密钥值。`HOST_BIND` 保持 `127.0.0.1`，外部访问通过 Nginx TLS；`PUBLIC_ORIGIN` 必须与浏览器实际访问的 authority 一致。

`USER_STATE_ROOT` 是每用户/会话隔离目录的父目录。实现完成后，目录名必须来自服务端认证主体的不可逆内部 ID，不能直接拼接 Cookie、用户名或 URL 参数。

## 分阶段接入

1. 契约采集已完成：使用 packaged CLI 的 `--profile web --host 127.0.0.1 --port 13090 --no-open`，提供临时独立 `DSH_HOME`。
2. 执行断网短期探针，验证根路径、原生模块资源、`/api` 认证和 `/api/remote.mux` 握手；通过后再做真正浏览器 JS 渲染验证。
3. 接入 SSO 到 Host 会话的服务端映射，再验证普通 DSH 对话和历史回写。
4. 完成项目/插件权限桥接后，才替换临时 React 验证页。

在第 2 步通过前，不应把 `/megmeet-ai-r10/` 子路径当作原生 DSH Host 的成功证据，也不应删除旧系统。

完整 `web` profile 自带 Agent 和会话执行。后续从模型服务、企业工具与历史事件层接入平台；一次用户对话不能同时由 Web Host 和 Python Adapter 再跑两次。现有 Gateway REST 对话保留作临时回归入口。原生 DSH 会话不会自动写入平台 PostgreSQL，也不会自动获得公司 SSO 权限。

## 当前测试服务骨架（2026-09-14）

本地已增加 `start.py` 和 `dsh-web-test` Compose profile，作为共享测试 Host：它在容器内 loopback 启动 DSH `web`，再由 3084 TCP bridge 提供给 Nginx。该 profile 不加入默认 `core`，需要先把已验收 runtime wheel 放入 `services/gateway/vendor-wheels`。

此服务只解决测试阶段的自动启动和重启恢复；生产仍需按用户或会话隔离 Host、服务端 SSO 映射、独立 `DSH_HOME` 和 secret 文件。
