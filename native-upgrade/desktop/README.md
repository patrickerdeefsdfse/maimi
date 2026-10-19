# 麦米桌面：DSH 0.2.1-rc.2 升级候选

本目录把公司登录、公司模型和已授权的 service 接入官方 DSH 桌面。普通聊天、文件与复杂任务默认使用用户本机的原生工作区和工具；不再插入独立“云端工作区”入口。企业接口与远程服务器操作仍需要公司网关。删除云端入口不代表全部企业业务已经迁移到本地。

这是整合中的候选源码，不是公司已部署版本。公司真实 IAM、最终 Windows 安装器、真实 SSH 及全量企业业务仍需验收；本地合成测试通过不能替代这些验收。

## 固定版本

| 内容 | 版本与来源 |
| --- | --- |
| 上游 DSH | `0.2.1-rc.2`，预发布 |
| 上游 tag / commit | `dsh-v0.2.1-rc.2` / `687f282e8625c6b8d11ff58211a90524be2e4317` |
| 公司此前正式桌面 | `desktop-local` 0.13.0 / DSH 0.2.0-rc.2；不能把本候选当作已发布升级 |
| 来源校验 | `upstream-source-lock.json` 与固定下载归档核对；不跟随 `latest` 或 `next` 浮动标签 |

`../upstream/runtime-win` 和 `../upstream/runtime-linux` 是精确依赖清单，安装后才有原生运行时。上游完整源码由 `scripts/fetch-source.py` 取得并校验，不提交依赖目录或下载归档。

## 桌面如何工作

1. Electron 主进程通过公司 IAM 登录。设备证明仅用真实 `safeStorage` 加密后保存；启动优先恢复记住的设备，首次登录、明确拒绝或过期时才重新打开公司登录页。安全存储不可用时只做本次登录，不降级明文。
2. 已验证的公司 access token 留在父进程。模型桥通过私有回环 capability 服务原生 Host；官方 profile、模型插件、Session、AttachmentStore、浏览器、Office 和任务退出能力继续使用上游实现。
3. 每个账号的候选状态按工号散列分隔。正常关闭保留记住的设备；“退出公司登录并关闭麦米”经过原生任务确认和锁，先禁止自动续登，再停止会话并尝试撤销设备。离线撤销保留恢复 journal，不把网络失败当作撤销成功。
4. 授权名单中的 service 用户能在原生对话输入区域主动启用连接区，仅填 **IP、密码**。SSH 用户为 root、端口 22；当前会话显式启用后，模型才可发现 `maimi_service`。普通账号不安装该客户端条目，也不注册该工具。
5. service 的 IAM、服务端控制/工具许可留在父进程闭包；原生 Host 只通过既有私有 IPC 获得本地 capability。密码不放进模型工具参数、聊天、profile、环境变量或持久配置。退出/换账号先取消桥请求并尝试退役连接许可，再关闭 Host；未取得确认时不宣称远程命令已停止。

## 代码地图

| 位置 | 负责内容 |
| --- | --- |
| `src/company-desktop-session.mjs`、`company-host-child.mjs` | IAM/设备续登、父子生命周期、账号隔离、私有 IPC |
| `src/company-auth/` | 公司 IAM、OS 加密设备存储、断网轮换与撤销 journal |
| `src/company-model-bridge.mjs` | 公司模型授权代理与取消；任务子进程不继承模型凭据 |
| `src/native-company-host.mjs` | 官方 profile 启动、内存插件配置、公开无秘密客户端条目 |
| `src/local-enterprise/` | 原生 Session/Peer 企业桥与 service 两字段输入区 |
| `src/company-shell-host.ts`、`patches/native-shell.patch` | 官方 Electron 窗口、任务退出、菜单及 Host 接线 |
| `src/upstream-native/` | 精确版本的上游任务退出/更新锁组件，保留 MIT 许可 |
| `scripts/` | 来源校验、独立 staging、补丁生成与构建准备 |
| `tests/enterprise/` | 原生桥、实际 Host/Peer、浏览器输入区的可复现测试 |
| `../../services/local-enterprise-tests/` | 网关授权、真实 Redis Lua、broker 持久退役回归 |

`src/native-desktop-ui/` 留作历史源组件，目前 profile **不加载其云端链接**。公司更新渠道尚未接好时，官方账户/自动更新渠道保持关闭；这不等于公司签名更新已经完成。

## 已验证与边界

- 设备续登的 27 项历史本地套件通过，包括真实 Host 双启动、合成 IAM、轮换/撤销、实际上游菜单变换、原生子进程凭据清除。真实 Electron OS 解密与公司 IAM 仍需实机验收，见 `monday-evidence/`。
- 企业桥原生回归 17 项、网关 10 项、broker 持久退役 5 项、隔离 Redis 7 Lua 回归已通过。测试调用本机合成公司端点或本地隔离资源。
- 实际 parent → fork Host → Cookie owner Peer → 两字段客户端 → 原生模型工具 → 合成服务端结果通过；验证退出取消、普通账号无 service、无 cloudHost 请求、密码不进历史/Storage/profile。浏览器把真实公开客户端挂到受控槽夹具中，**不是完整 Electron GUI 验收**。
- 上游完整 Linux 构建门禁已有通过记录。Windows 安装器整合及后续结果以最终构建回执为准，不能从上述测试推导安装包已验收。

证据和运行命令见 [企业桥接线与测试](tests/enterprise/INTEGRATION.md)、[后端授权测试](../../services/local-enterprise-tests/README.md)。`original-validation.json` 保留原候选证据；仓库迁移后的复测单独记录，不改写历史结果。

## 开发与构建

需要 Node 24、Python 3.11+；Windows 安装器在 Windows 构建。以下以本目录为工作目录，先安装与系统一致的精确运行时：

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm --prefix ../upstream/runtime-win ci --ignore-scripts --no-audit --no-fund
python scripts/fetch-source.py
node scripts/native-build.mjs inspect
node scripts/native-build.mjs stage --output "$((Get-Location).Path)/native-source"
python scripts/prepare-native-shell.py
npm test
```

Linux 使用 `../upstream/runtime-linux`。原生测试支持 `DSH_R65_RUNTIME`；新增实际桌面 Host 测试优先 `DSH_R65_DESKTOP_RUNTIME`，未设置则使用前者或系统对应的相邻 runtime。企业浏览器测试额外安装 Playwright/Chromium，命令见接线文档，不包含在默认 `npm test` 中。

`stage` 只接受不存在的新目录。对已验证的独立 staging 应使用 `prepare-native-shell.py --stage-root <绝对路径> --receipt-directory <独立回执目录>`；不要覆盖安装目录或用户状态。正式构建在 staging 中按固定 pnpm 锁执行：

```powershell
Set-Location native-source
$env:DSH_CLIENT_COMMIT_HASH = '687f282e8625c6b8d11ff58211a90524be2e4317'
node ../node_modules/pnpm/bin/pnpm.mjs install --frozen-lockfile --ignore-scripts
node ../node_modules/pnpm/bin/pnpm.mjs --filter '@deepseek-ai/dsh-desktop' exec install-electron
node ../node_modules/pnpm/bin/pnpm.mjs run build:artifacts
node ../node_modules/pnpm/bin/pnpm.mjs --filter '@deepseek-ai/dsh-desktop' run build
```

Office primary runtime 沿用上游资源定位器；开发启动器可提供 `DSH_DESKTOP_PRIMARY_RUNTIME_DIR`，不自造固定 payload 目录。Host 合成测试未安装完整 Office payload，不证明 PPT/浏览器/字体在最终安装器中的质量。

## 仍需完成的企业能力

- 工卡、会议、图纸等 project 工作流的本机会话授权不能靠删除 `PROJECT_CLOUD_REQUIRED` 放行，目前未接成桌面功能。
- 通用 connector/远程 Office 底层桥有合成测试，但桌面未暴露其未完成的选择与写操作确认界面。当前只启用实际接线的 service。
- 公司加密 Office 文件仍需获准的解密上传入口。底层桥明确拒绝 TSD 密文，不宣称自动解密；本地普通 Office 能力另属上游。
- service 的 10 分钟短期许可到期后需重新连接并输入密码，尚无保持 SSH 不断开的滚动续期。远程 root 命令是服务器管理能力，不是隔离沙箱。
- 公司签名更新/回滚、真实用户历史和附件恢复、完整安装器及公司业务验收仍是发布门槛。`activation-gate.mjs` 的全功能门禁继续保守，不能用 service 单项通过替代所有验收。
