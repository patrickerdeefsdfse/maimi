# 麦米服务端

此快照的组件版本见 [release.json](release.json)，发布源码文件及摘要见 [source-map.json](source-map.json)。

| 路径 | 职责 |
| --- | --- |
| gateway/app | 公司登录、模型转发、上传、文件清理、云工作区、桌面更新分发和管理员入口 |
| gateway/app/observability | 节点、服务、用户活动、队列、历史统计和图表 |
| gateway/maimi_projects | 工卡、会议、图纸与成本、监控和企业工具路由 |
| gateway/migrations | 业务库迁移；独立监控表见 deploy/postgres/history-schema.sql |
| manager/personal-cloud | 工作区状态、Kubernetes 调度、空闲回收、共享文件边界 |
| worker/personal-cloud | 就绪检查、公司 PG 历史适配、模型凭据隔离、工具和文件代理 |
| worker/dsh | Linux DSH 锁文件、公司插件适配、工具、企业技能、PPT 和 Office 适配 |
| office | 已维护仓库中的独立共享 Office 服务和引擎参考实现；当前工作区通过 worker 内的适配器接入 |
| tests | 在发布执行镜像内验证会话历史恢复的合成测试 |

## 构建和维护

网关以 server/gateway 为 Docker 构建上下文。门户自带默认背景与图标。部署方可在 MAIMI_PORTAL_ASSETS 挂载 wallpaper.jpg、logo.png、background.png、background-alt.png 来应用站点素材。管理员和项目服务的员工名单由 MAIMI_AUTHORIZED_STAFF_GROUP_1 和 MAIMI_AUTHORIZED_STAFF_GROUP_2 配置，搜索设置名可定位对应授权点，留空时不授予相应权限。员工照片接口由 MAIMI_PROJECT_PHOTO_URL 配置。IAM、PG、Redis、模型及业务服务凭据通过私有配置提供。

管理程序与工作区分别以 server/manager 和 server/worker 为构建上下文。两个构建均提供 chat（默认）和 execution 目标。执行池的覆盖源码单独保存在对应的 execution-overrides 目录。它们从信任的发布镜像按摘要加载底层引擎和二进制资源，然后复制受维护的服务代码。维护时应保留原基础镜像的来源与摘要。若从通用 Node 基座全新组装，需要准备 Linux amd64 Node 24、按 worker/dsh/package-lock.json 安装依赖，提供锁中 dsh-files 的私有 tarball，并复原相应的工具链、Office 和插件资源。

公司 UI 的可维护 JavaScript 在 `worker/dsh/plugins/company-ui/client.template.js`。工作区 Dockerfile 的 `company-ui` 阶段从已安装的公司客户端读取两份授权品牌图片和展示用的地址样例，按清单摘要验证重建的 `client.js`。同一阶段运行 `worker/patch-native-chat.py`，让原生聊天在显示回复时隐藏销售订单复核表的控制标记。该脚本可识别已验收的 rc.5 原版和已打补丁的版本，并校验最终文件摘要。升级到其他上游版本时，需重新验收原生渲染并更新摘要。最终镜像从该阶段复制两份客户端文件。修改公司 UI 时还要更新网关 `connection_repair.py` 中的资源摘要白名单并验证双 AP 真实插件请求。完整五机部署另依赖受控镜像、独立基础设施、授权的企业服务和证书。

工卡、会议、成本和企业技能的处理器在 gateway/maimi_projects 与 worker/dsh 中。公司 PPT 模板、会议签名文件、OCR 模型、字体及文档二进制依赖通过企业制品库提供。office 中的旧独立服务源码用于开发和迁移参考，未将其描述为当前集群已运行的单独服务。

历史恢复可用实际执行镜像以 Node 为入口运行 server/tests/history_contract.mjs，禁网、根文件系统只读并以只读卷挂载测试文件。测试使用合成事件，不需要真实员工账号、聊天或附件。


## 站点配置

企业凭据由 Secret 或只读挂载文件提供。安装方按当前 IAM 工号设置以下受控值。

| 配置项 | 用途 |
| --- | --- |
| `MAIMI_PLUGIN_OWNER` | 可管理其他管理员的唯一工号 |
| `MAIMI_INITIAL_PLUGIN_MANAGERS` | 首次部署的插件分发管理员，多个工号用逗号分隔 |
| `MAIMI_AUTHORIZED_STAFF_GROUP_1` 和 `MAIMI_AUTHORIZED_STAFF_GROUP_2` | 开发监控和项目运维各自的授权名单 |
| `MAIMI_MONITOR_SERVICE_CIDR` | 用于核对 Kubernetes API 地址的站点 Service 网段 |
| `MAIMI_MONITOR_CA_SHA256` | 验证集群 CA 的 SHA-256 |
| `MAIMI_MONITOR_A1_UID` 和 `MAIMI_MONITOR_A2_UID` | 验证两路监控 ConfigMap 的实际 UID |
| `MAIMI_WINDOWS_INSTALLER_SHARE_URL` | 站点维护的 Windows 手动下载分享链接 |

员工号格式为字母 M 加八位数字。授权名单留空时默认没有成员。部署模板中的示例域名及站点网络值应换为本环境实际配置。多个副本使用相同的授权配置和监控身份固定值。

## 源码校验范围

网关第 93 代、订单服务第 23 代、聊天管理器第 54 代、执行管理器第 50 代和 Office 第 5 代按已部署副本核对。工作区镜像分别核对了聊天池 126 个、执行池 125 个程序文件。其余开发文件作为构建输入保留。维护镜像使用对应池的固定基础镜像提供底层依赖。

源码清单的 SHA-256 对应 Git 中存储的字节。与正式副本逐项比对过的文件带有 `compared_with` 和 `matches_reference_bytes`。两份摘要可能因为站点配置提取、素材替换或换行标准化而不同。Windows 的独立锁定清单见 [desktop](../desktop/README.md)。
