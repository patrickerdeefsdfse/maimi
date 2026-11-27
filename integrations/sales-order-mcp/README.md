# 麦米销售订单

使用《销售订单接口(1).docx》的三个原始 FastAPI 接口执行上传、WebSocket 识别和结果下载。业务服务接收来自麦米已登录会话的工号及姓名，使用独立的平台令牌调用正式服务。识别行支持原字段定位、员工更正和版本绑定复核。返回的 Excel 原件按麦米会话权限提供下载。公司保护的文件保留原样。

识别环境由管理员在业务服务配置 `ORDER_EXTRACT_URL`，普通 `recognize` 直接执行，不再生成额外的环境选择闭环。当前联调部署显式使用 `https://service-3.example.invalid/v1/mc-saleorder-extract/noauth`。正式服务为 `https://service-4.example.invalid/v1/mc-saleorder-extract`，须配置 `ORDER_EXTRACT_PLATFORM_TOKEN`。模型不能选择任意URL或通过识别参数切换环境，状态卡只返回受控环境名和配置完整性，不返回令牌。所有已登录员工默认可用，显式撤销继续生效。麦米网关与内部订单服务使用各自专用认证，始终保留员工、会话及附件归属，确认建单必须由员工本人操作。旧版页面的环境按钮仅为兼容保留，不作为模型工具公开。

Oracle 建单依次调用 `parse_xml_data`、`validate_data` 和 `create_order`。员工确认当前版本后才可写入。成功需返回 S 和订单号，随后进入“待业务审核”。独立审核查询必须返回与订单号及业务实体一致的通过状态，才可将原件传到鸿翼。待审、拒绝、未配置、超时均不会上传。业务键与归档键在所有员工之间去重，写入后状态不确定时需人工核查。启用数据库和鸿翼的参数见配置样例。

归档目标已由业务负责人选择销售订单专用位置：[100 示例销售档案](https://archive.example.invalid/folders/9000001)。这是企业文档目录的网页引用，不能作为 `collectArchives` 的规则 ID 或授权令牌。客户基础资料、销售物流资料的两条采集规则及其令牌不用于销售订单。必须等后续业务审批通过后才提交原件。

该位置下有 `01 示例部门甲`、`02 示例部门乙`、`03 示例部门丙`、`04 示例部门丁`、`05 示例部门戊`、`06 示例部门己`、`07 示例部门庚`、`08 示例部门辛` 八类。已有三家 Oracle 业务实体分别精确对应前三个子目录。其他主体的完整业务实体名称须明确配置，未知主体不会自动落入“其他主体”。`hongyi.destination` 保存这些确认事实及实际 API 目录映射，父目录和子目录 `folder_id` 初始为 0，核对真实 API ID 与父子关系后填写。已确认的导航引用为 `9000001`，不凭网页链接推断上传接口参数。

当前 V3 适配器按订单业务实体选择子目录，在重复检查、创建文件及回执中使用同一目录。实际元数据字段由该目录适用的表单决定，`form_id` 和 `metadata_fields` 须填真实映射，不强制远端具有订单号、PO、客户账号及 SHA256 四个字段。原件 SHA256 和唯一归档台账继续在麦米内部检查。缺失的映射源字段会在网络请求前报错，查询已有文档不要求上传映射。若专用归档采用 `collectArchives`，需提供销售订单自己的规则 URL、字段与成功回执，当前 V3 配置不能直接换成 JSON 采集 URL 使用。

尚需对接的最小信息：审批查询地址与通过值；上述目录的实际 API 父子 ID；适用的上传协议和服务凭据引用；该协议的表单或规则字段及成功回执。服务状态会展示目标目录和主体分类，目录已确认不代表上传接口已配置或启用。

复核表逐行保留原件识别到的单价和含税口径。只有缺价的行才查最近符合条件的 Oracle 历史单价。调用 `lookup_price` 时，已有价的行直接返回当前值；缺价的行如无历史价，可查询已配置的客户价目表。提交人可以在复核表中修改单价，确认后继续建单。

`lookup_customer` 可查询客户名称对应的账号候选。原件缺少客户 PO 时，`generate_po` 可按客户和日期生成内部编号。查询和更正均绑定当前员工、会话和订单。

`status` 返回受控的 Oracle 环境、期望地址、端口、服务名、配置缺项，以及审批和归档接口是否配置。部署使用 `ORACLE_ENVIRONMENT=test` 或 `production` 标记环境，用 `ORACLE_EXPECTED_DSN` 登记管理员核准的目标。每次建立连接前解析 `ORACLE_DSN` 并与期望目标比较。状态和建单预览只展示目标标识与配置状态，账号、密码及原始连接串留在服务端配置中。变更数据库环境时须同步核对环境标识和期望目标。

驱动默认为 Thin。使用旧密码验证方式的测试账户需要 Oracle Client 时，由管理员配置 `ORACLE_DRIVER_MODE=thick`，并部署包含官方客户端依赖的专用订单镜像。Linux 客户端库在进程启动前由动态库路径加载，应用完成目标核对后才初始化客户端。切换驱动模式后重启订单服务。只读预检返回数据库连通与过程可见性，页面的配置完整状态不代表数据库已连通。

运行生产入口使用 `uvicorn order_mcp.cloud:create_app --factory`。持久卷中的会话订单与去重记录须使用同一存储。测试套件使用虚构数据及替代 Oracle 和鸿翼，验证授权、回执和审核门槛。


2026-11-11 试点交付。正式集群的网关连接 `sales-orders.maimi-production.svc.cluster.local:8765`，A1 的单副本服务使用独立持久卷。日常业务使用 Secret `sales-orders-integrations` 的 `platform-token`。接 Oracle 需在同一 Secret 配置 `oracle-dsn`、`oracle-user`、`oracle-password`、`oracle-write-enabled=true`，并在销售服务的出站策略允许批准的 Oracle 目标 IP 及端口。在配置就绪后重新启动销售业务服务，以载入 Secret 的环境变量。数据库账号需有业务方批准的 `CUX_BLUE_AI_OE_ORDER_PKG` 执行权限。多行 XML 契约经 ERP 方核对后可配置 `oracle-multiline-confirmed=true`。

用户在工作区「技能」选择「销售订单识别与归档」，在消息中上传图片、PDF或Excel，调用 `recognize` 即按管理员配置直接识别，结果可下载标准工作簿并复核。审批和归档参数不阻止识别或复核，多行契约只限制多行提交。复核通过后点「预览订单」可下载同版Oracle XML，点「确认创建 Oracle 订单」才发起建单。2026-11-11验收使用虚构附件，两个网关副本识别一行并读回Excel与XML，未调用真实Oracle写入或鸿翼上传。后续业务审核查询、鸿翼参数由接口方另行配置。

## ERP 管理员接入与只读验收

1. 由 ERP 管理员通过公司的密钥管理流程维护 `maimi-production/sales-orders-integrations`。数据库连接使用 `oracle-dsn`、`oracle-user`、`oracle-password`。独立的 `oracle-write-enabled` 使用字符串 `true` 才允许人工确认后写。日常正式订单的 AI 平台访问使用 `platform-token`。账号须能通过当前 schema 或本地同义词解析建单包。员工工号由已登录麦米会话交给过程。
2. 当前 `sales-orders-private` 规则允许访问集群 DNS 与识别服务的 HTTPS。增加 `maimi-production/sales-orders-to-oracle`，来源只选带 `service-2.example.invalid/component=sales-orders` 标签的 Pod。目标使用 ERP 管理员批准的 Oracle 实际监听地址，每个 IP 使用主机掩码（IPv4 /32 或 IPv6 /128），指定实际 TCP 端口。在 RAC 场景登记业务实际用到的 SCAN 及重定向监听地址。若数据库侧有限制，按网络团队核准的实际源地址放行。
3. `tools/oracle_network_policy.py --ip <监听地址> --port <监听端口>` 生成独立的 NetworkPolicy JSON。可重复传入 `--ip`。使用生产集群部署通道进行 server dry-run、审核并发布该规则。发布后检查销售服务可达，不扩大现有网关、工作区或其他业务的出站权限。
4. 订单 Pod 通过 `env.valueFrom.secretKeyRef` 载入数据。首次创建 Secret 或变更其中值之后，要在 `maimi-production` 执行 `kubectl rollout restart deployment/sales-orders`，等待 `kubectl rollout status deployment/sales-orders`。独立网络策略修改本身不要求重启。挂载的 PVC 保留复核记录和幂等状态。
5. 从销售业务容器执行 `tools/oracle_preflight.py`。在有正式 kubeconfig 的管理主机，与本项目源码同目录时可运行以下命令。它执行只读事务，仅检查连接和过程目录，不输出密码、连接串或客户记录。过程可见与 DB 账号的具体执行授权可由 ERP 管理员核对，真实建单由试点用户另行确认。

```sh
kubectl -n maimi-production exec -i deploy/sales-orders -- /opt/order-env/bin/python - < tools/oracle_preflight.py
```

预检返回码 0 表示只读连接可用且三个过程均可见；2 表示列出的连接字段未配置；1 表示连接或元数据验证未完成。

示例员甲业务回归命令：

```sh
python -m pytest -q tests/test_adapters.py tests/test_approval.py tests/test_orders.py tests/test_web_workflow.py
```

回归覆盖解析、验证、建单的顺序及失败停止，建单后的独立审批、待审/拒绝/超时/未配置禁传、授权撤销不归档、跨员工重复提交和归档结果不明时不重传。数据库预检和示例员甲回归均不向 ERP 创建业务单据。

## 部署用销售主数据

示例主体、客户、测试账号和网络地址用于本地演示。部署时可将销售主体对应的订单类型和仓库写入 JSON 对象（每个值为两个字符串组成的数组），用 `ORDER_ENTITY_DEFAULTS_FILE` 指定其路径。用 `ORDER_REVIEW_ENTITIES_FILE` 指定可供复核的主体名称 JSON 数组，另用 `ORDER_ALTERNATE_TYPES_FILE` 指定经核准的例外订单类型（以主体为键，值为类型名称数组）。

归档前由部署方核对目录并用 `ORDER_ECM_VERIFIED_PARENT_REFERENCE` 指定可信父目录参考编号。仅在复核测试库例程摘要与回执规则后配置 `ORDER_VERIFIED_RECEIPT_TEST_HOST`、`ORDER_VERIFIED_RECEIPT_TEST_PORT` 和 `ORDER_VERIFIED_RECEIPT_TEST_SERVICE`。数据库写入和员工审批沿用各自的授权流程。
