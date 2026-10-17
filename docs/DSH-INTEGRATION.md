# DSH 接入边界

更新：2026-09-17。新客户端是 Windows 桌面壳中的原生 DSH 界面，旧自写 React 页面已退役。当前可运行链路及锁定版本以 [`dev-local/dsh`](../dev-local/dsh/README.md) 和 [`desktop-local`](../desktop-local/README.md) 为准。

DSH 负责原生会话、Agent、模型协议和运行时插件；企业服务负责可信身份、授权、个人密钥托管、审计与业务任务。原生核心与企业适配分别版本化，不把企业权限写进上游界面，也不将客户端插件选择当作授权。

## 当前本地链路

桌面访问回环地址，经本地 Nginx 转发到独立 DSH 容器。DSH 的状态和默认工作区使用独立持久卷；本机模型与公司接口均为合成测试服务。Gateway 在另一路径运行，普通原生聊天不重复调用 Gateway 的 Python Adapter。

原生版本由精确依赖锁和镜像记录固定，品牌适配是独立构建层。升级后需验证启动、原生页面、模型协议、工作区与历史恢复；不能只验证版本号就假定企业插件兼容。

## 企业接口与兼容路径

[`packages/contracts/chat.ts`](../packages/contracts/chat.ts) 保留 Gateway 请求和历史摘要纯类型，没有网络或 SSO 副作用。原生企业选择器与执行桥接尚待实现，Gateway 企业执行分支当前仍为 501。

`services/native-entry` 保留服务器个人 Host/SSO 入口与续期适配；这不等于本机桌面已经接入真实公司登录和个人 Key API。SSO 身份必须由可信服务端确认，客户端不能决定工号、组织或资源权限。

`services/gateway/app/dsh_adapter.py` 保留已有 REST 兼容和回归入口。其 SDK profile、独立 `DSH_HOME`/workspace/HOME/cache 与 provider 映射仅适用于该路径。本机 Gateway 的 SDK 开关保持独立，DSH 原生升级不要求把 Gateway 重建成捆绑运行时的大镜像。

`services/dsh-web-host` 与主 Compose 的 `dsh-web-test` 是服务器历史兼容部署路径，本次前端清理未修改它们。服务器 wheel 版本、部署结果和真实账号证据仍在 `codex-context`；不可用旧文档中的一次失败推断当前服务器状态。

## 模型与权限

公司模型方向是通过可信 SSO 身份取得员工对应 TokenKey，由后端代理使用；客户端不接触真实 Key。默认模型为 `deepseek-flash-v4.1`，企业插件的其他模型必须在授权目录内。个人 Key API 契约与真实调用尚待接入。

原生协议及 provider 能力按已锁版本验证，显式映射平台路由与上游模型；不接受客户端提供任意 base URL、profile、运行时路径或密钥。旧 React 客户端的任意 401/403 自动刷新后重放请求不再复用。
