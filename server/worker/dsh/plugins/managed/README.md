# r73 固定插件与两端适配

本目录为本机候选，未操作公司服务器或当前桌面安装。2026-10-26 核对官方发布及 npm：next 为 0.2.1-rc.3，latest 仍为 rc.2；继续锁定 rc.3。

| 插件 | 固定版本 | 候选行为 |
|---|---|---|
| dsh-context | 0.77.0 | 两端统计；保留 r72 余额、价格、全局提示词、附件与会话保护 |
| dsh-files 作者 Git | 0.6.2 | 仅 read_document；Web 转交 C，桌面 Office/企业 PDF 优先 wps-read |
| GenUI | 0.14.0 | 两端图表/表格/流程图，交互状态只在内存，不自动加载外部媒体 |
| 麦米轻量配色 | r73 | 四种配色，本机仅保存枚举，无背景文件上传 |
| better-sidebar | 0.28.0 | 仅桌面文件/Git/预览，保留受管任务权限 |
| BrowserSkill | 0.3.3 | 桌面包与适配已安装；未配对不启用执行工具 |
| dshmarket | 1.70.2 | 目录参考，禁止动态安装 |
| ModLens / ModSearch | 3.27.2 / 5.10.10 | 固定安装，未接通公司视觉/搜索渠道 |

any-background 0.4.1 已核对并安装参考包，但原版支持大视频与长期背景存储，不挂载完整界面；以麦米轻量配色替代。GenUI 与 visualize 重叠，仅启用 GenUI。未叠加 vision-router、Univer、WeKnora/dsh-knowledge、DingTalk/DWS、dsh-im、TUI 或 web-all；这些涉及重复功能、独立长期存储、额外后台或企业通信渠道，不能假装已经接通。

## 文件与历史边界

dsh-files 不注册原版附件扫描、附件库、导出复制和删除 HTTP 路由。最大文档 24 MiB、每次 400 行、最多 16,000 字符（部分格式更低）。Web 只接受规范化后位于 /workspace/ 的路径，调用 ctx.get('maimiExecution').readDocument(args,{sessionId,signal})。缺少代理明确报错，不在 A 本地解析。

C 导入生成目录 files/index.mjs 的 executionDocumentTool(ctx)，调用 .execute(args,exec)。ctx.fs 提供 resolve/stat/readBytes，ctx.emit 接收 fs/observed；exec.agent.session.header 提供真实 id/cwd，exec.signal 支持取消。执行节点继续负责权限与软链接边界。

桌面 Office 文件与默认 PDF 必须先用 wps-read。用户明确确认普通未加密 PDF 才可传 unencrypted:true；它不会让 Office 跳过 WPS。此插件不代替 WPS 对真实加密文件的现场验收。

Context 不复制附件或请求公共余额/价格。GenUI 表单与面板不持久化第二份业务内容、不自动请求模型修复，刷新由原聊天重放；聊天仍由 PG 按 14 天规则处理。原附件和输出生命周期仍由主项目 NAS 账本管理。

## 可重建交付

dependencies/package.json 与 package-lock.json 为两端共同安装输入。把 vendor/ 复制到安装根 vendor/ 后运行 npm ci --ignore-scripts --legacy-peer-deps。29 个宿主 peer 已显式固定；legacy 用于 BrowserSkill 作者版本范围尚未覆盖 rc.3 的声明，不自动引入旧 DSH。BrowserSkill 的本机程序、浏览器扩展配对及实际操作尚未验收。

vendor/dsh-files-0.6.2-119bff5.tgz 来自作者提交 119bff5ef462ea9e4d8c0ba5d12c2bfc03b041e0，不是同名 npm 占位包。source-lock.json 与 npm lock 记录摘要和下载来源。

构建器核对版本及源文件 SHA256 后生成独立目录，不改原始 npm 包：

    node /opt/dsh/plugins/managed/build-managed.mjs --runtime /opt/dsh --out /opt/dsh/plugins/managed/r73

输出目录必须不存在。桌面打包应复制本目录源文件及 context/sidebar/files/genui/browser/appearance 子目录，不沿用旧版固定七文件清单。Linux 从锁重新 npm ci，禁止复制 Windows node_modules。Dockerfile.dependencies 仅是依赖层，业务镜像与现场 PG/NAS 验收独立进行。

证据在候选根 plugin-validation/：实际 rc.3 Host、Chromium 加载、GenUI 合成表格、外部媒体拒绝、Web 文件代理无 A fs 访问、WPS 优先、上游实际解析及下载/权限回归。没有将 import 成功当作 BrowserSkill/视觉/搜索真实渠道已验收。