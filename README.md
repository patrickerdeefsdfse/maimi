# 麦米办公助手

麦米为员工提供浏览器云工作区和 Windows 本地客户端。员工通过企业 IAM 登录，使用已获授权的办公工具、销售订单和工卡技能。管理员可批量分发或收回插件，查看设备同步和实际使用回执。

此源码快照对应 2026-11-27。Windows 客户端为 **0.15.10**，内置 DeepSeek Harness **0.2.2-rc.5**。Linux 网关、任务管理器、办公服务和销售订单服务各自发布，版本分别记录在服务端清单中。

| 目录 | 内容 |
| --- | --- |
| [server](server/) | 登录网关、管理与监控、云工作区、文件处理、办公服务和企业工具 |
| [desktop](desktop/) | Windows 桌面壳、安装与内部更新、原生插件、运行时定制和测试 |
| [integrations/sales-order-mcp](integrations/sales-order-mcp/) | 文件识别、可编辑订单复核、Oracle 建单与审核、鸿翼归档接入 |
| [integrations/dingtalk](integrations/dingtalk/) | 钉钉与员工身份关联、消息接入及路由 |
| [deploy](deploy/) | 网页入口、集群、数据库高可用和共享存储的部署模板 |
| [docs](docs/) | 架构、运行维护和更新说明 |

**部署准备。** 从[服务端说明](server/README.md)和[五机部署说明](deploy/README.md)准备基础镜像、站点地址、存储和企业接口。员工授权名单、企业凭据、客户主数据和实际员工照片由部署环境提供。Windows 构建和内部更新源的设置见[桌面构建说明](desktop/README.md)。公司安装包通过站点配置的下载入口分发。
