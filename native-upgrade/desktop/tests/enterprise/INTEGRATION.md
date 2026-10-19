# 桌面本机企业桥：接线与复现

当前源码已合入 `../../src/local-enterprise/`，由 `company-desktop-session.mjs` 管理父进程授权与停止，`native-company-host.mjs` 在真实 profile 中注册服务端插件和公开 `service-ui`。没有临时 overlay 依赖；本目录所有测试直接导入仓库业务文件。

只有已授权工号可安装 service 客户端，默认未启用；交互 owner Peer 主动选择后，真实 root Session 才获得 service 工具。模型不能提供 employee、device、session 或附件 ID 来冒充授权；不能调用 enable/connect 或读取密码。自定义 RPC 使用 `HostConnectionService.register(ctx, ...)`，显式声明 connection/webServer 依赖，完整 Host 不依赖抽象测试中的宽松 Context。

父子私有 IPC 传递本地 capability，不放环境变量/profile。公开插件路径可见但没有秘密。当前实际桌面仅公开 `maimi_service`；`bridge.test.mjs`/`native.test.mjs` 中通用 connector/Office 是底层能力测试，不代表这些入口已向员工开放。

## 服务端配套

- `services/gateway/app/local_service.py` 的 router 由网关 main 注册；复用已有桌面 Bearer `/v1/connectors/` 范围和真实 IAM/Redis desktop grant，不放宽旧云端授权。
- 服务端依据可信员工、设备、IAM session 和 desktop grant 生成 10 分钟控制/工具双许可，替换/撤销不允许旧代复用。
- `services/service-broker/` 增加私有 `DELETE /v1/connection-grants`。SQLite `retired_connections` 的 hashed scope 在 ACK 前 FULL 提交，保留 900 秒覆盖已签发 wire scope 最大 665 秒。登记前/发布前/get 均检查；普通 disconnect 仍可同代重连。
- broker 私有 journal 必须持久保存；首次启动增加表，不涉及 Postgres。网关与 broker 必须配套部署。超时/取消后的连接结果不确定时也退役；确认丢失返回 `REMOTE_STOP_UNCONFIRMED`，不能称远端任意命令已结束。

## Node 测试（在 native-upgrade/desktop 目录）

安装 Node 24 与对应系统的精确 DSH 0.2.1-rc.2 runtime。以下测试支持 Linux/Windows；本次实际复测的平台记录在 `repository-validation.json`，不把路径可移植等同于两系统均已实测。

```text
node --test tests/enterprise/service-native.test.mjs
```

该命令通过测试依赖加载 bridge/native/service 共 17 项，只使用真实 DSH 公共服务与本机合成 HTTP。默认 runtime 是相邻 `../upstream/runtime-win` 或 `runtime-linux`；已有独立安装可以配置：

```powershell
$env:DSH_R65_RUNTIME = 'C:/isolated/runtime-0.2.1-rc.2'
$env:DSH_R65_DESKTOP_RUNTIME = $env:DSH_R65_RUNTIME
```

```bash
export DSH_R65_RUNTIME=/absolute/isolated/runtime-0.2.1-rc.2
export DSH_R65_DESKTOP_RUNTIME="$DSH_R65_RUNTIME"
```

浏览器测试另外需要 Playwright 1.62.1 和它的 Chromium。可安装到单独测试依赖目录，不改业务 package-lock：

```text
npm install --prefix .test-browser --no-save --package-lock=false playwright@1.62.1
node .test-browser/node_modules/playwright/cli.js install chromium
```

如需 Linux 系统浏览器依赖，在获准的测试机按 Playwright `install-deps chromium` 安装；测试本身不装系统包。然后配置可选覆盖并运行：

```powershell
$env:MAIMI_PLAYWRIGHT_MODULE = "$((Get-Location).Path)/.test-browser/node_modules/playwright"
node --test tests/enterprise/host-service.test.mjs
node tests/enterprise/service-ui.mjs
```

```bash
export MAIMI_PLAYWRIGHT_MODULE="$PWD/.test-browser/node_modules/playwright"
node --test tests/enterprise/host-service.test.mjs
node tests/enterprise/service-ui.mjs
```

默认使用 Playwright 已安装的 Chromium；已有审核过的浏览器可设 `MAIMI_CHROMIUM_EXE` 为绝对路径。测试不会探测个人浏览器或读取用户 profile。`MAIMI_DESKTOP_TEST_SRC` 可选指定独立构建源码；默认直接测试 `src/`。完整 Host 使用 `DSH_R65_DESKTOP_RUNTIME`，其次 `DSH_R65_RUNTIME`。

Host 测试通过真实 launch URL/cookie、owner Peer、公开 profile 的组合客户端资源与原生模型工具执行；UI挂载在受控 slot fixture，通过实际 Host RPC。验证两字段、5.2 秒刷新保留输入、连接/查询、账户退出取消、普通账号无 service、密码不进入历史/Storage/profile、无 cloudHost 请求。没有完整 Electron GUI、公司 IAM、SSH或业务调用。

## 启动失败资源回收

独立回归 `node --test tests/session-startup-cleanup.test.mjs` 对真实回环桥监听器验证 fork 同步异常、两桥建立期间 abort 的回收；abort 后不启动子进程。该修复由集成审查加入，未改写原冻结候选证据。

## 发布边界

本地接线/回归通过，不等于安装器或公司服务已上线。其余企业 project 技能本机授权、connector 写确认、公司加密文件上传、service 滚动续期尚未完成。原候选验证见 `original-validation.json`；仓库改路径后的结果另存 `repository-validation.json`。不要把历史数值当当前每次测试自动生成的回执。
