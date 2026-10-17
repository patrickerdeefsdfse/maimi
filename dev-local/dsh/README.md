# DSH 0.1.6-alpha.1 本地桌面运行核心

按用户要求接入最新官方发布版，精确 npm 版本 `@deepseek-ai/dsh@0.1.6-alpha.1`，不是 npm `latest` 标签。2026-09-17 查询时该标签仍为0.1.5-rc.1，alpha标签才是0.1.6-alpha.1。官方预发布页：https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.1.6-alpha.1 。

## 运行结构

Windows桌面程序（`../../desktop-local`）→ 回环28084 → 本地Nginx → 独立DSH容器 → 本地模型模拟器。原生DSH界面保留，Gateway仍独立在28090；DSH升级无需把Gateway重建为含DSH的大镜像。桌面开发版目前依赖这台电脑的MegmeetDev WSL环境，不是供全员分发的独立安装版。

DSH状态和默认工作目录位于专用`dsh-data`卷的`/state`，不挂载Windows用户目录、Docker socket、公司密钥或真实历史。当前为一个本地开发用户；公司SSO、按工号个人Host路由和个人TokenKey领取未接入此开发入口，不能将此入口公开部署。

## 固定与适配

- `package-lock.json`固定所有npm依赖和integrity；镜像通过`npm ci`安装。
- Node22.20.0镜像固定digest。官方npm版自带HMR要求`--expose-internals`，启动器只给DSH子进程增加该标志。
- 新版默认模型协议改为Messages。`profile.patch.yaml`明确指定`chat-completions`，保持已有TokenKey接口形态；本地只调用模拟服务。
- `prepare-runtime.mjs`核验全部236个DSH包均为0.1.6-alpha.1，生成静态前端/原生UI模块的资源摘要与版本清单。按本轮新要求运行独立`brand-overlay-r2.mjs`：1.png首屏人物、2.png侧栏图标、MEGMEET、文案“电亮AI新纪元”，去Preview；同时保留去内测弹窗适配，该处校验官方文件SHA256后只去除welcome-notice注册，不改模型设置和凭据提示。
- `launcher.mjs`启动DSH、在内部完成认证、预创建唯一默认工作区，再提供桌面本机入口。它不实现公司认证，不向浏览器暴露DSH临时token。
- 默认工作区由实际原生UI自动选中；本轮没有隐藏其他DSH功能。

## 开发与验收

Windows包装器 `../../ops/local-env/dev.ps1 start`启动现有镜像，`native-smoke`检查原生入口/工作区/Host和Origin边界，`restart-dsh`仅重启DSH。有意修改运行核心后，在MegmeetDev中运行本目录`rebuild.sh`，只构建和重建DSH。

2026-09-17已重新验证实际提供的静态JS/CSS和原生UI模块摘要、全部DSH版本一致、无内测弹窗、自动工作区、桌面真实输入并获得一轮流式模拟回复。原有测试历史在容器重建后保留。最新Windows开发客户端为0.1.3，它的版本号独立于DSH。模拟器回复以`LOCAL_MODEL_OK`标识，不是实际DeepSeek推理。服务器原0.1.5rc2、两个真实试用账号、真实SSO/TokenKey均未改动。本次未验证工具执行、新版图片Files API、容量、公司插件/PTC迁移或生产个人用户隔离。

后续升级顺序：选定官方版本→更新整个npm锁→核对插件接口/协议及品牌和notice适配→验收实际前端资源/会话与重启→生成运行镜像和版本清单→再做公司环境验收。不能只换CLI或前端dist；DSH原生客户端模块与Host应同版。当前镜像为`megmeet-dev-dsh:0.1.6-alpha.1-upstream-ui`，源码及完整发布清单记录在`../locks/runtime-manifest.json`。旧megmeet1镜像已删除。
