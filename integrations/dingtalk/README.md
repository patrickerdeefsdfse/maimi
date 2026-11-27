# 麦米 · 现有钉钉机器人接入

**交付状态为 QA 候选，正式开关默认关闭。** 此目录包含 MagicAPI 私聊分流、经鉴别的接收接口、员工 IAM 绑定、限时模型委派、慢问答任务与本地验证。没有生产账号、凭据或机器人消息。它复用公司现有机器人，没有新建机器人或占用另一条 Stream 连接。

## 建议阅读顺序

1. [QA 回调自检与慢问答](QA回调自检与慢问答.md)。先用一条专用 QA 指令核对原始 HTTPS 回调的验签结果。
2. [MagicAPI 接入](现有钉钉机器人接入.md)。在原有脚本提取 `wt` 后插入 [私聊分流片段](MagicAPI-麦米私聊分流.ms)。原物流、姓名日期等处理仍在原处。
3. [员工绑定与委派](员工私聊委派QA.md)。员工在已登录的麦米浏览器配对，然后授权最长 15 分钟的纯文字问答。EIP 与钉钉不持有个人模型密钥。
4. [管理员接入说明](管理员接入.md)区分公司统一机器人、桌面个人微信扫码及 DWS。微信公司统一通道仅有身份隔离准备，本目录交付的 MagicAPI 入口只接钉钉私聊。

## 运行契约

- 接收器入口 `magic_ingress:create_local_service` 使用项目私有网关基础镜像中的 `app.iam_directory`、`app.iam_oauth`、`app.personal_cloud`、`app.personal_model_proxy` 和 `app.desktop_auth` 等实际模块。依赖 PostgreSQL 16、psycopg、cryptography 和 FastAPI；需要网关侧原有服务配置。不可脱离这些受限依赖在一台通用 Web 服务器上直接公开。
- 身份与回执表为 `identity-schema.sql`；启用员工模型委派时同时需要 `delegation-schema.sql`、`jobs-schema.sql`。部署凭据应独立于应用数据库的 DDL 权限。
- `company-im.example.json` 仅为默认关闭的交接模板。运维按说明提供企业、机器人、受限内部服务地址、密钥挂载及 IAM 配置。私有材料只能通过批准的秘密管理系统分发。
- UI 和 API 源码仅在开通 QA 模型委派时提供。异步回复为员工主动取件。结果未知不自动重放，不向未验证的群聊或好友发送内容。
- 部署前在现场验证回调模式、私有网络、双员工隔离与真实个人授权；代码发布本身不会连接或登录任何机器人。

## 合成验证

`qa_pg_checks.py` 使用 Docker 创建一个临时的无外部网络 PostgreSQL 库，并运行路由、绑定、入口鉴别、双员工模型取件与任务恢复测试。设置 `MAIMI_QA_GATEWAY_IMAGE` 指向本机已有且包含以上网关模块的镜像，`MAIMI_QA_POSTGRES_IMAGE` 默认 postgres:16。不会拉取镜像、使用业务库或调用真实模型。

`qa_magic_check.py` 在 Java 17 中编译分流片段，使用临时合成密钥和内存 HTTP 捕获器，不登录钉钉也不发送消息。请将测试用 MagicScript 1.9.0、MagicAPI 2.2.2、Spring 5.3、Fastjson 等 JAR 的 Java 类路径设置到 `MAIMI_MAGIC_TEST_CLASSPATH`。

```sh
python qa_pg_checks.py
python qa_magic_check.py
```
