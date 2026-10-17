# r62 组件源码映射与集成边界

该快照把已发布 GitLab 基础源与 r54/r56/r59/r61/r62 的公开源码叠加到可阅读的位置。它没有复制生产宿主文件系统，也不包含当前私有环境值、登录状态或员工数据。

| 组件 | 开发源位置 | 运行位置 / 装配说明 |
| --- | --- | --- |
| 企业网关 | `services/gateway/app/`、`services/gateway/maimi_projects/` | 容器 `/app/app/`、`/app/maimi_projects/`，在原网关镜像上受控挂载增量 |
| 云端管理器 | `services/personal-cloud/manager.py` | `/opt/personal-cloud/manager.py`；需同时配置完整原生覆盖目录与发布来源校验 |
| DSH worker | `services/personal-cloud/worker.mjs` | `/opt/personal-cloud/worker.mjs`，与运行时依赖/覆盖整组校验 |
| 原生覆盖 | `runtime-overlays/worker/` | 文件相对该目录的路径对应容器绝对路径；不是完整 rootfs，不能用它单独构建运行时 |
| 工具协议 | `toolkit/` | 与 worker 内 `/opt/dsh/toolkit/` 对应的发布文件保持一致 |
| service broker | `services/service-broker/` | 独立容器内 broker 源码，依赖 asyncssh 及既有 FastAPI/Uvicorn/cryptography 基础镜像环境 |
| 公司 PPT 模板 | `runtime-overlays/worker/opt/dsh/company-ppt-template/` | 用户授权提供的原始公司模板、manifest、技能与辅助程序；模板保留原文件哈希 |
| Office 服务/引擎 | `services/office-service/`、`office-engine/` | 使用独立 Office Python 环境，转换器与字体需随运行时制品管理 |
| 桌面 | `desktop-local/`、`desktop-build/` | 正式 0.13.0 的应用与构建适配资料；没有自动发布新的安装器 |

## 为什么不是直接 compose up

现场版本由逐代基线检查、公开源码挂载、受控运行时制品、私有配置、Nginx 路由以及维护事务共同形成。本次源代码快照没有把生产容器 inspect、私有配置或发布回执入库。根 Compose 来自早期基础模板，没有自动包含 service broker、r62 全部挂载或当前入口配置。历史发布清单保留原指纹，不可删掉校验强行执行。

首次完整部署需要补齐：完整镜像/运行时制品来源和 digest、网关及 broker 的基础依赖镜像、数据库初始化、身份/模型/连接器配置、密钥挂载、持久卷、Nginx、当前完整 Compose、启动/恢复演练与业务验收。`services/service-broker/requirements.txt` 只声明新增 asyncssh 依赖，不是整个基础镜像的完整依赖锁。

## 配置、权限与示例

现有生产源里的固定员工授权常量和内网路由属于项目代码，保留原字节用于追溯。它们不包含密码，也不等于新环境的可用权限配置。应先在开发分支把部署授权外置到经校验的配置，再重建运行时和来源锁，而不是在备份时悄悄替换常量导致源 pin 失配。不得把私有数据库、凭据文件或员工附件提交到此仓库。

本次快照没有新增 CI/CD 自动发布。后续适合先建立离线语法/单元测试、来源/依赖检查、模拟环境集成与手动批准的环境晋级，再接生产部署。

## 实验源码

- r63：会话卡片可见性修复候选，仅收录变更后的原生品牌覆盖；还需完整发布组装和公司业务验收。
- r64：任务预算、作用域、取消、失败保留预算、OpenSandbox 适配等开发源码；不得当成完成的沙箱服务启动。真实 runsc 节点和 DSH 全执行世界替换未验证。

实验目录不会被当前根 Compose 或 r62 覆盖自动加载。

## 导出时的唯一源差异

物流技能说明中的历史发货单示例沿用已审查的虚构示例，避免把业务参考号分发到 GitHub。该说明对应的两个 runtime source pin 和 manager 文件清单哈希已同步为本导出版本；其他产品运行逻辑及授权常量保持原样。原始 r62 发布包未修改，本导出不是原包的字节级副本。源码来源记录区分该派生变更，重新发布应生成新的完整版本包。
