# 麦米桌面：DSH 0.2.1-rc.2 原生升级候选

本目录是隔离开发候选，不是已发布桌面安装器。没有修改用户已安装的麦米、公司服务器、冻结的 0.13.0 源码或旧更新包。

## 版本事实

| 项目 | 已确认事实 |
| --- | --- |
| 公司正式桌面来源 | GitHub 冻结源码 `desktop-local` 为 0.13.0，使用 DSH 0.2.0-rc.2 |
| 最新本地旧候选 | r54 目录里的 0.17.0 未发布；其更新 feed 未签名/发布，不能当正式版本 |
| 新上游 | 0.2.1-rc.2，tag `dsh-v0.2.1-rc.2`，commit `687f282e8625c6b8d11ff58211a90524be2e4317` |
| 发布通道 | 最新预发布；npm `next` 指向它，`latest` 仍是旧版；本候选精确锁版本，不跟浮动标签 |
| 官方源码核验 | 16,283 个普通文件，133,199,860 字节，均与固定 SHA256 的上游 tar 比对 |

`upstream-source-lock.json` 包含每个普通文件的哈希和 14 个上游符号链接记录。Windows 新 staging 暂不重建这些链接；它们主要是文档别名和测试夹具，不能声称完整上游测试已经执行。上游完整源码在同级 `../upstream/source`，共享目录只读。

## 已写入并实际验证的代码

- `src/native-company-host.mjs`：通过官方 `loadProfileDirectory`/`runProfile` 启动新 Desktop profile，使用原生 base/Web bundle、原生连接认证、原生模型插件。没有复制/替换旧版 UI bundle。
- `src/company-model-bridge.mjs`：公司 access token 留在父进程；原生 Host 只得到短期随机 loopback capability。模型路由与授权目录匹配，身份代次/退出/过期检查在发送与接收时执行；错误不透传凭据或网关原文。
- `src/company-desktop-session.mjs` / `company-host-child.mjs`：接上现有公司 IAM PKCE/浏览器验证码登录与续期，使用官方 `ready`、`injections`、`shutdown` IPC 形式；按工号散列分离候选状态。拒绝没有候选标记的既有状态目录，避免接触安装版用户历史。
- `src/company-auth/`：复用已发布 0.13.0 的 IAM 协议实现；保留设备续登协议代码，但本候选尚未接上 Electron safeStorage 设备凭据界面。
- `src/company-shell-host.ts` 与 `patches/native-shell.patch`：已把上述登录、模型桥和 HostReady 接入官方 Electron `main.ts`。保留官方窗口、浏览器、目录选择、加载页和任务退出逻辑；候选使用独立 appId、userData 和 DSH_HOME，关闭官方账号入口与官方升级渠道。补丁修改 3 个上游文件，另一个修改是导出上游已有的 Desktop Office 组合与引擎路径解析器，供公司 Host 原样调用。
- `src/upstream-native/`：沿用锁定版本原样的任务退出检查及更新任务准入控制；保留上游 MIT 许可。真实 Host IPC 已验证检查、锁定和解锁。
- `src/native-desktop-ui/`：只新增原生 `sidebar.footer.action` 槽中的云端工作区浏览器入口。默认仍是官方本地工作区；此入口不携带公司凭据。
- `src/native-build.mjs`：精确源码校验、独立 staging、冻结 pnpm 安装与官方打包命令计划。默认不启动安装器、不签名、不发布。
- `src/activation-gate.mjs`：公司实测前不能发布；本机会话技能授权、上传、下载、取消及不依赖云 Host 都通过前，不能移除当前产品的云入口。

合成网关实际完成：新 profile 启动 → 官方一次性 URL 换 cookie → 原生 UI HTTP 200 → `ctx.llm.stream` 经原生 pi-ai/父进程公司桥获得 SSE 回复 → 官方 shutdown。另以两个合成工号顺序启动，验证账户目录分离、退出后旧 URL 不可用。测试没有访问公司服务、没有输入真实工号凭据、没有业务模型调用。

## 运行本地验证

需要 Node 24、Python 3.11+、Windows x64。当前本地验证为 **32 项通过**，包括真实最新版 Host、原生模型流式调用、退出检查/更新锁、IAM 续期，以及 Electron 主进程适配器测试。Electron 适配器使用模拟系统接口；完整 Electron GUI、正式打包和公司登录尚未通过实机验收。

以下命令在本目录执行。它们只构造隔离候选，不启动公司部署、不写正式安装目录。

```powershell
npm ci --ignore-scripts --no-audit --no-fund
npm --prefix ../upstream/runtime-win ci --ignore-scripts --no-audit --no-fund
python scripts/fetch-source.py
node scripts/native-build.mjs inspect
node scripts/native-build.mjs stage --output "$((Get-Location).Path)/native-source"
python scripts/prepare-native-shell.py
npm test
```

`fetch-source.py` 固定下载地址和 SHA256；已有源码不覆盖，随后 `inspect` 核对全部普通文件。`stage` 只接受不存在的新目录，第二次复用已生成 staging 时直接执行 `prepare-native-shell.py`，不要删除安装目录。`scripts/seal-source.py` 可从固定 tar 重新核验生成相同源码清单；日常使用已提交的锁即可。

原生构建仍按上游锁文件执行，必须在 staging 内进行：

```powershell
Set-Location native-source
$env:DSH_CLIENT_COMMIT_HASH = '687f282e8625c6b8d11ff58211a90524be2e4317'
node ../node_modules/pnpm/bin/pnpm.mjs install --frozen-lockfile --ignore-scripts
node ../node_modules/pnpm/bin/pnpm.mjs --filter '@deepseek-ai/dsh-desktop' exec install-electron
node ../node_modules/pnpm/bin/pnpm.mjs run build:artifacts
node ../node_modules/pnpm/bin/pnpm.mjs --filter '@deepseek-ai/dsh-desktop' run build
```

安装和原生完整编译不是 32 项测试的一部分。首次 Windows 安装在下载完成后发生 `EPERM rename`，缓存/copy 重试也未完成导入，因此转到 WSL ext4。Linux 已用同一锁文件完整安装，并通过上游 `build:artifacts` 的 7 个门禁：native addon、Host TS、Host bundle、Desktop bundle、Client TS、Client bundle、Web build。这个结果不等于 Windows 安装器通过；公司实测、Office primary runtime、企业本地技能桥及签名更新尚未完成，不发布安装器。

Linux 编译可运行 `bash scripts/build-wsl.sh`，该脚本固定归档 SHA256、在新 `$HOME/maimi-r65/desktop-native` 中应用相同补丁并安装冻结依赖；也可用绝对路径 `MAIMI_DESKTOP_BUILD_ROOT` 指定新的构建根目录。脚本不会覆盖已有 staging。安装后进入该 staging，设置 `DSH_CLIENT_COMMIT_HASH` 为上述精确提交，再执行 `node ../desktop-build-tools/node_modules/pnpm/bin/pnpm.mjs run build:artifacts`。这只是编译验证，不会启动公司登录或桌面窗口。

提交源码时包含本目录的 `src/`、`scripts/`、`tests/`、`patches/`、锁文件和说明；同级保留 `upstream/runtime-win/package*.json`。不要提交 `node_modules/`、`native-source/`、`build-tools/`、下载归档和候选用户状态。

## 原生替代范围

| 当前定制 | 新方案 | 当前状态 |
| --- | --- | --- |
| 混合 Web bundle/加载页覆盖 | 官方桌面 packaged loading page + 原生 Host ready IPC | profile/IPC 已实测；完整 Electron shell 尚未构建验证 |
| 固定端口/手动占位端口 | 官方 Host 动态回环端口 | 已实测 |
| 普通会话、文件、技能、插件管理 UI | 官方最新 base/Web/UI 插件 | 原生 UI 已提供；公司业务交互另验收 |
| 普通 Office/渲染环境 | 官方 Desktop Office + primary runtime；公司模板/服务作补充 | 原生接口已接线，未改 Office 实现；此 Node smoke 未安装完整 Desktop Office payload |
| 自制桌面浏览器/目录选择/任务关闭行为 | 官方 Browser、系统目录对话框、托盘、退出任务检查 | main.ts 已接线，原生任务控制通过；完整 GUI 待验收 |
| IAM/公司模型代理 | 独立父进程公司登录与模型桥 | 协议合成测试和实际 native profile 请求已通过 |
| 企业技能/service/工卡/物流/模板 | 单独公司插件与经授权文件桥 | 与 Root 的独立公司插件对接，尚未宣称桌面业务可用 |
| 公司签名更新 | 保留公司 Ed25519 更新信任、回滚与版本策略 | 尚未接入官方更新 UI，不能启用官方渠道替公司发布 |

## 完整 Electron 集成还差什么

1. 完成 Windows 目标依赖安装/打包，执行真实 Electron 窗口与公司 IAM 登录、会话过期重登、关闭任务提示验证。当前 Linux 完整编译、主进程接线与模拟系统接口已通过，但不能替代 GUI 验收。
2. 准备官方 primary-runtime payload，对已接线的 `office.ts`、`office-engine.ts` 验证系统路径、Python/PPT 渲染及浏览器实际操作。本轮 Host 测试不包含完整 Desktop Office payload。
3. 公司独立 UI/企业技能在原生本机会话完整验证（账号授权、输入上传、结果下载、取消及无云 Host 依赖）；通过之前保留新增云端入口和当前产品入口。
4. 在候选副本验证原生历史继续对话/附件、升级失败恢复；本轮只用全新隔离历史，没有迁移真实用户资料。
5. 迁移公司设备安全存储、签名更新/回滚、品牌与安装器，执行 Electron 实机测试后再形成正式安装包。关闭原版官方更新并不等于已接好公司更新。

没有把 `PASS_SYNTHETIC` 当作 `PASS_COMPANY`。当前入口/公司服务器未部署，正式桌面仍保持原状。
