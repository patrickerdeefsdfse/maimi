# r65 gateway 与 DSH 0.2.1-rc.2 接入记录

日期：2026-10-18。基线为 GitHub 已发布源 3e69e21 对应的本地两个冻结导出。候选只在本地验证；没有公司服务器登录、部署、业务查询、员工聊天或附件读取。不能据此判断 M00003308 的线上故障原因。

## 实际修复

| 候选文件 | 修复与证据 | 范围 |
|---|---|---|
| `app/main.py` | `/ready` 的 psycopg `dict_row` 查询使用四个唯一列别名，检查精确列集合和每个值；旧实现遍历字典键，且未命名列被折叠，可误报 READY。16 个回归包含旧误报复现。 | 不改变 IAM、业务查询或迁移流程。 |
| `app/personal_cloud.py` | 对原生 RPC 媒体类型按分号分割、去空格和小写，再进入保留/删除规则，和新版原生解析一致。 | 防止 `Application/JSON` 被原生接受但漏过外围规则。二进制上传不进入此分支。 |
| `worker-adapter/task-control-plugin.mjs` | 新版 `TypertGateway.invoke` 必须提供存活 `Peer`。给私有 prompt/cancel 控制入口建立插件生命周期内的 owner Peer，`interactions: none`。 | 不从模型或浏览器接收 principal；插件卸载后 Peer 失效。 |
| `worker-adapter/enterprise-model-policy.mjs` | 保留新版 `prepareCall(config, signal, configure)` 的第三个回调；旧包装会吞掉原生请求控制配置。 | 公司模型路由和任务凭据限制保持。 |
| `worker-adapter/worker.mjs` | 原生 RPC JSON 媒体类型判断与上游一致。旧判断忽略大小写合法的 JSON 请求，使路径约束没有执行。 | 本候选相对基线只有一行；与其他 worker 改动合并时应用该行差异，不能覆盖别人的组装改动。 |
| `vendor-wheels/.gitkeep` | 保留 Dockerfile 既有 `COPY vendor-wheels` 的目录。 | 仅修复 Git 不跟踪空目录；不代表业务镜像已完整可复现。 |

## 新版原生 API 核验

上游证据是 `deepseek-ai/deepseek-harness` 源提交 `687f282e8625c6b8d11ff58211a90524be2e4317` 与同版本 npm `0.2.1-rc.2`。测试使用已安装的真实包，不用同名替身模拟其契约。

- `api/gateway/src/types.ts` 的 `InvokeRemoteRequest.peer` 为必填；`index.ts` 在解析描述符前读取 `peer.signal`。本地重现旧入口失败，并让修复入口实际经过原生 Gateway 的描述符、参数校验和 Service 绑定。
- `client/connection/src/peer.ts` 的 Peer 由创建者固定 principal，挂在 Context 生命周期下。`owner` 是单个 DSH Host 的拥有者身份，不是公司 IAM 管理员身份。公司代理必须仍先证明此 Host 属于当前员工。
- `llm/llm/src/index.ts` 的新 `configure` 回调，在捕获模型元数据后、默认值解析前运行。测试确认回调执行一次、修改的 maxTokens 生效，取消信号仍生效。
- `agents.get/list/roots/isOwnedBy` 与模型流作用域所需的接口兼容。测试使用原生 AgentRegistry，验证跨根会话和已撤销代次被拒绝。
- `credentials-local` 新增 record 能力，且文件中的普通 reference 优先于环境变量。保留原生能力，但 `MEGMEET_MODEL_TOKEN` 始终由本次流作用域解析，拒绝手工设置或删除；没有上下文时不降级到环境 Key。本地 SSE 测试确认实际 PiAi 请求只收到合成任务票据。未调用任何真实模型或凭据。
- `attachments.readFileStream/saveFileStream/readImage` 与现有附件桥兼容。测试经过真实 AttachmentStore 和 Session，核对用户接受的附件证明、字节/哈希、生成文件往返；模型伪造的附件引用不能取得读取授权。
- `Session.snapshotEvents` 在新版仍可用，但已标为过时接口。当前附加桥是既有读法，保留兼容；后续应迁移到上游正式异步日志读取边界，不能贸然换成未确认稳定的接口。
- 新版 `user/message` 写入要求 `surfaceOp`。我们的文件桥只读取原生接受记录，没有手工写入；测试按新规范构造记录。其他自行写 Session 的代码须单独检查。

`worker-adapter` 中 `native-model-scope`、`native-scoped-credentials`、`native-task-context`、`project-files-plugin`、`project-file-transfer-gate`、`task-authority` 是供隔离测试的未改基线副本，无需覆盖相同项目文件。

## 原生连接与 Peer 边界

1. 新版普通 RPC 继续走 HTTP `/api/<namespace>/<method>`；`remote.mux` 负责流式 Remote 和事件。Gateway 会在打开流时拒绝 unary 方法，不能把普通 prompt 直接塞进流调用。本地源核验没有发现这种绕过，但完整浏览器链路仍属于整合验收。
2. worker 为已验证的公司请求替换原生 Host、Origin 和本进程 Cookie。原生浏览器 Cookie 映射 owner Peer，`interactions: answer`。每员工独立 Host 下这一设计成立；不能把多个员工直接并到同一个 owner Peer。
3. 私有 `/api/megmeet-cloud/` 控制入口必须继续同时被 gateway 路径检查和 worker 代理拒绝。只有 wrapper 在原生回环内调用。此次新增 owner Peer 依赖这一既有边界；不能将它注册成公开工具、公开 RPC 或接收用户提交的角色。
4. 上游 `$events` 交互流与 `$events/result` 回答要求同一个 Peer，且必须是 `interactions: answer` 的 owner。不能把私有控制 Peer 当成浏览器交互 Peer，也不能为兼容而关闭 `remoteAccess` 或省略 Peer 检查。
5. 新 `/connect?token=...` 是原生启动令牌交换，不是公司登录 API。不要向员工暴露原生启动令牌，或用它替代 IAM/租约校验。现有 wrapper 使用自己的已建立浏览器 Cookie，不需要在前端暴露该令牌。
6. 云端 task-authority 仍明确限制跨登录自主计划、部分动态插件写操作和额外工作区。这些是既有公司策略，不能为“最新版功能更多”一键移除。应逐项接入可撤销身份、任务生命周期和文件范围后开放。桌面本地能力与公司云端策略应分开。

## 桌面企业能力脱离云 Host 的接入界面

不能删除 `PROJECT_CLOUD_REQUIRED` 后直接透传 local path。现有架构存在两种不同状态：

| 能力 | 当前真实边界 | 迁移处理 |
|---|---|---|
| 企业 connector catalog/invoke/confirm | `enterprise_connectors.py` 已通过 `require_trusted_actor` 支持 IAM Cookie 或桌面 Bearer grant，并在调用前后重新检查身份；物流无需个人云 Host。 | 优先复用已有接口，接新版本地工具插件。 |
| 公司 Office API | `office_remote.py` 的公开入口通过可信身份建立员工/会话 scope；内部入口才使用云租约。 | 复用公开入口，文档上传/成果下载继续受 scope 约束。 |
| 工卡、会议、成本等 project actions | `project_platform.py` 依赖 cloud lease、owner、worker origin/route-token 代次、native session、选中技能许可、数据库 epoch/fence、任务记录。 | 需要明确新的本地执行访问对象，不能仅忽略 worker 绑定。 |
| project 输入/成果 | 当前从真实原生用户消息获取附件证明，通过受控 worker FilePort 读写，再核对 hash/bytes 和发布许可。 | 为桌面建立独立授权文件端口；不得让服务端直接打开模型提供的 `C:` 路径、URL 或任意附件 ID。 |
| service | 密码由专用连接区域送到受控 broker；模型只有任务/连接能力。UI ticket 目前绑定 session、选择许可和代次。 | 新桌面表单仍只收 IP/密码；本地会话凭证与 broker scope 明确绑定，凭据不进模型消息和日志。 |

建议的最小新界面：桌面在 IAM 认证后申请短期企业会话许可，服务端绑定员工、桌面实例、本地原生 session、技能选择版本、允许动作、过期时间和取消栅栏。工具发起任务时使用此许可与显式附件 manifest；模型只能提供业务参数。输入从用户接受的附件经过公司规定的上传协议进入 scoped 文件存储；服务端核对格式、大小与摘要，产生不可猜的文件 ID。产物以相同作用域登记和下载，不反向暴露服务器目录。任务执行与幂等、状态、取消、发布授权独立于某个云 worker 是否存活。

这部分是已明确边界的待实现接口方案，本候选没有开放任何未经验证的新授权通道。完全去掉桌面云 Host 依赖前，应通过三个实际项目技能的本地上传、执行、取消、断线重连、跨用户隔离和成果下载验收。

## 干净构建修复与验证

本候选的 Dockerfile 已明确复制 `maimi_projects`，补齐源码实际导入的六项业务直接依赖，并固定完整 Linux 依赖版本及 Python 基础镜像摘要。包版本来自既有本机 gateway 镜像元数据；没有添加任意新包。`vendor-wheels/.gitkeep` 保证默认无私有 wheel 也可构建，最新版 DSH 仍在 Node worker 中装配。

已在本机 Docker 从固定基础镜像重新安装包并完成干净构建，`pip check` 通过。实际镜像导入 19 个业务子模块，合成 XLSX 经工卡解析器读取、DOCX 往返、二维码 PNG、PDFium 栅格化均通过。一次性内部网络中启动真实 PostgreSQL/Redis/网关，验证 `/health=200`；`/ready` 对空库、缺表、缺迁移均为 503，齐全为 200，随后删表再次为 503。测试对象均已清理，没有公司调用。

素材/字体/模板仍需要受控部署挂载；不声称这些公司资产已经内置，也不声称员工全业务验收完成。wheel 的内容哈希尚未加入依赖锁，当前锁定包版本和基础镜像摘要。完整构建说明和复现实验见 `services/gateway/README.md` 与 `tests/gateway-image-smoke.py`。

## 验证与仍待验收

运行 `python -m unittest discover -s gateway/tests -p 'test_*.py'`，`node --test gateway/tests/worker-adapter.test.mjs`。Node 测试通过 `DSH_R65_RUNTIME` 指定真实 npm 安装根，默认使用同级 `upstream/runtime-win`；测试只重定位固定 `/opt/dsh/node_modules` 前缀。Node 24.13.0 下通过；临时文件只存合成内容。

此次覆盖私有控制器到原生 Gateway 的真实边界，但 Session 命令实现使用合成 Service，未声称是完整 AgentLoop prompt→真实模型→企业任务全链路；完整 worker 启动/原生浏览器/插件 UI/上传解密/企业身份现场验收应由整合阶段继续执行。公司线上尚未运行本候选，M00003308 仍需原安排的只读连接诊断。
