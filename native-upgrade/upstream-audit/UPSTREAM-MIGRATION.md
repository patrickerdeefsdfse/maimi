# DSH 0.2.1-rc.2 上游核验与迁移边界

核验日：2026-10-18。本文以官方 tag 源码为依据，用户转交的聊天仅作问题线索。公司未执行任何升级；实验安装均位于本地隔离目录。

## 固定候选

- 最新发布：`dsh-v0.2.1-rc.2`，commit `687f282e8625c6b8d11ff58211a90524be2e4317`，GitHub 标记 prerelease，2026-10-17 08:16:33 UTC 发布。
- npm `@deepseek-ai/dsh` 的 `next` 指向 `0.2.1-rc.2`，`latest` 仍为 `0.2.0-rc.2`；必须精确锁定版本，不能使用 `@latest`。
- CLI：`@deepseek-ai/dsh/lib/bin.js`，支持 `dsh --profile web` / desktop profile；`./profile-boot` 是受支持组合入口，禁止直接运行任意内部 package bin 代替 profile。
- Release API 当前 assets 为空，源码由 commit 固定的 codeload 包取得；可运行组件通过 npm 精确版本组合。不要把源码压缩包当作已构建的 Windows 安装器。
- 对照基线：`dsh-v0.2.0-rc.2` → `639ed015397290b3745d163aafe02ffee4aa3f84`。旧研究目录 `dsh-rc2-research-20260928/source/rc.2` 的实际 CLI 版本为 0.1.7-rc.2，不能用该目录名证明它是当前旧版本；此次另取正确 tag 源码。

## 原生能力与麦米处理决定

| 能力 | 源码核验 | 迁移决定与限制 |
|---|---|---|
| 会话、历史和恢复 | `core/session/src/types.ts` writer=4；`docs/session-format-status.md` 已发布基线也为4；`api/session-controller`、`client/connection` 和 `session-query` 管恢复 | 使用原生会话/列表/重连。仍需从真实旧格式的合成fixture验证续聊、文件引用、取消。相同writer数字不等于允许线上降级，更不能把新用户加载故障直接判为已解决。 |
| 工作区入口 | New Session 总创建新Agent，openWorkspace/connectWorkspace可复用空白会话；API网关现有principal/interaction权限 | 保留公司身份到个人目录的映射；不覆盖原生新会话/重连逻辑。对自定义bridge更新调用契约。 |
| Skill和插件 | `skill/skill` provider registry、`skill-filesystem`、`client/ui-skill`，插件启停刷新、`engines.dsh`兼容判断 | 原生列表、选择、/菜单、安装/HMR优先；物流、工卡、公司模板等增量保留为插件/skills。移除整个官方UI bundle替换模式。 |
| MCP | `mcp/mcp-client/src` 支持stdio与streamable-http、稳定工具namespace、发现/取消/重连；无默认外部server | 通用MCP交给原生；快递100等企业凭据和限额仍在网关受控后端，不分发给浏览器/模型。不可把缺少MCP后台误作所有技能失效。 |
| 普通子代理 | `subagent`+`tool-subagent` 返回activation/subagentId，执行归runtime管理；持久化和父级完成通知 | 采用原生委派、等待、继续、打断；不要继续维护旧job-backed/foreground结果。独立SDK运行时需要session/wait。 |
| Agent Team | `experimental/agent-team` 直接投递Agent inbox，持久task board；profile含UI与tools | 可选实验能力，不声称通用分布式调度。限定同进程、同工作目录；需durable Session。默认profile为8成员，service默认16；团队消息没有outbox/自动重试/重发去重。 |
| 浏览器 | `browser-use`仅单provider登记；Playwright MCP、Chrome DevTools MCP、Stagehand均独立experimental provider，自己管理Session浏览器 | 优先使用一个官方provider；实际浏览器依赖、企业出网和跨用户隔离未验收前不默认启用全部。该registry本身不提供浏览器动作。 |
| Office | `skill-office`现在7个skills，生成/编辑与render/recalculate/export指导按需加载；runtime提示只追加给后三类 | 使用原生Office指导与工具入口；保留公司模板和已有Office企业后端，禁止覆盖所有原生skill正文。cli:false仍合法，明确表示部署禁用Kit。模板服务不等于官方Kit依赖完整。 |
| SSH/service | `ssh/ssh/src/index.ts` 要POSIX客户端、已配置OpenSSH alias和已安装helper的路径+hash；node-script/executable两种launch，禁止模型选身份 | 上游SSH helper不能直接替代用户要求的网页IP+密码连接：它不提供密码表单/临时任意IP登录，而且Windows客户端被拒绝。企业service broker、权限/凭据隔离继续保留，界面改独立原生扩展slot。 |
| 桌面 | `apps/desktop`正式profile入口、动态端口、PTY回收改进；desktop依然完整Web+本机runtime | 麦米桌面保留公司登录、模型授权、文件桥；一般聊天/工具/技能UI由新版原生承担。需要专门Windows打包测试，不拿Linux成功当桌面成功。 |
| 沙箱 | `sandbox/sandbox`明确为same-world进程文件权限；`shell`/`fs`/`subprocess`/`sandbox`必须同一执行世界 | 本地基本限制用原生；r64容器任务池仍承担跨用户、资源/网络/环境隔离，不能拿原生file fence替代。新版契约核对后再接入，禁止只换shell却让fs/PTC留在宿主。 |
| 搜索、模型、定时 | 0.2.1-rc.2有Flash-1017和DeepSeek Search/Fetch；web原生schedule内置 | 原生通用功能优先；模型需公司授权/路由验证，不能未经确认切换全部员工模型。Search/Fetch需凭据/额度/网络，配置存在不代表已可调用。 |

## 必须处理的破坏变化

1. **Web监听/信任**：删除`webRuntime`，改`webStartup`；connection inject和trustedHosts表达式同步改。`0.0.0.0`/`::`监听被明确拒绝，应选容器具体IP；`--public-url`负责展示/打开/模型URL，不自动赋予信任，反代仍要`--trusted-host`。来源：`docs/upgrade-guide/v0.2.1-alpha.1/web-listener-trust-config/guide.md`、`bundle/web-app/src/startup.ts`。
2. **连接参与方式**：非页面客户端默认`interactions:none`，只能generated unary/raw streams；要答复交互需connect时显式answer。浏览器cookie/owner路径未因此要求重认证。来源：`docs/upgrade-guide/v0.2.1-alpha.2/operator-connect/guide.md`、`api/gateway`。
3. **凭据优先级**：Web/Desktop优先credential文件，随后profile `.env`、cwd `.env`、继承环境。Headless默认走环境，不读已保存key。企业模型代理不得被旧个人配置遮蔽；不要打印这些值。来源：`credentials/credentials-local/src/index.ts`、stored-credentials-precedence guide。
4. **输入区扩展**：旧`conversation.input.stats`整行扩展拆activity与usage；新增用户消息、新会话和模型选择器slots。麦米品牌/配额/技能入口移至公开slot，不能套旧压缩client bundle。来源：`client/ui-conversation/src/client/contract/slots.ts`及apply.ts。
5. **子代理与Team**：移除tool-subagent.backgroundMode/enableRunInBackground、model run_in_background和in-process-driver包；Team移除outbox/retries/dedupe。旧配置字段必须删；完成通知不得按旧jobId解析。来源：subagent-activations、team-direct-inbox guide及对应源代码。
6. **持久化插件导入**：JSONL从默认class改为namespace的name/Config/apply，不再new/default/instanceof。存储文件格式与service接口保持；卸载消费者须等待收尾。来源：jsonl-persistence-plugin guide。
7. **Tool presentation**：只剩native/ptc，both删除。Python PTC必须有sandbox、sandboxPolicy；Node PTC可选persistent-in-memory，取消/超时/权限变更会清掉运行状态，不能当成持久数据。来源：tool-presentation-mode、python-ptc-sandbox guides、ptc-runtime-node源码。
8. **生命周期**：Session插件records使用实验API；保留新session代际和原生scope/disposal语义。API方法消费者必须处理RemoteResult，别用本机service调用形状猜远程RPC。
9. **配置/插件**：invariant插件及./invariant导出已删除；子路径display metadata应由export提供；plugin CLI的--profile必须置于pnpm透传参数前；--from-default-profile重建会替换现有profile/settings，不能对员工目录直接执行。
10. **指令与目录**：agent-instructions逐行dshHome去除，使用进程DSH_HOME/DSH_AGENTS_HOME；工作目录通过working_directory/Session接口维护，不能只变process.cwd后假定全会话已切换。

## 交付验收边界

Linux 隔离 npm 安装已完成（scripts 启用，615 packages）；CLI 实际返回 0.2.1-rc.2，10 个关键模块 import 成功。native flock 实际加锁成功，Node SQLite FTS5 实际建表检索成功。本机 WSL Landlock 探测为 partial，不能承诺 full 文件隔离。

正确旧 tag 的 51 份归一化官方 v4 snapshot 经新旧两个真实 npm Session reader 对照：双方均恢复 46 份，5 份均因 fixture 缺工具声明或图片 projection 上下文而拒绝，拒绝信息逐项相同；通过的 46 份原事件前缀完整保留。只补齐 snapshot 刻意省略的 seq/time 和 cwd 占位，不修改消息。两版 writer=4，SQLite 派生索引 schema=8。此项验证是核心恢复，不是完整 Agent 续聊，更不是公司历史已验收或安全降级承诺。参见 runtime-check-linux.json / runtime-check-linux-baseline.json。

官方 libreoffice-kit 0.1.5 的 Linux WASM 模块已随完整安装提供。官方 color-blocks.pptx 实际转 PDF 成功，随后由 PDFium 实际转 864×576 PNG 成功，converter 正常 dispose；PDF 转换约 9.8 秒。试样缺 Arial（明确报告），不代表公司模板字体问题全部解决。产物与证据见 office-kit-smoke-linux.json。

完整候选 Web worker 已在本机 Docker 隔离运行，启动约 9.34 秒。真实 session/create→prompt→企业 scope ticket/lease→合成模型 SSE→原生会话历史通过；合成中文文件上传的文件名、SHA256 与实际字节完整保留，附件进入持久消息且路径传到模型；其他 lease 不能取消本任务，合法取消关闭上游流，随后新代际 prompt 成功。使用 `network none`、容器 loopback 假网关，5 次合成模型请求，无公司调用。证据见 `worker-business-tests/business-result.json`。这不等于公司安全网关的实际解密已通过。

最新真实 ChatView、ChatFlow、SlotRegistry 与 Cordis Context 下，独立 company-ui 插件的 service 两字段卡片分别用 native/PTC 合成轨迹验证通过：显示可输入的 IP/密码、提交后清除密码、连接状态与会话绑定、回放不发连接。真实 Host plugin loader 也登记并返回该客户端模块。见 `native-ui-tests/native021-service-ui-results.json`、`host-plugin-discovery.json`。此测试不包含公司网页完整启动、浏览器缓存或真实 SSH。两份企业 skill 的 `dsh-skill` peer 版本更新至 0.2.1-rc.2 后，由最新真实 SkillRegistry 注册及读取通过，补丁见 `enterprise-skill-manifest-patch.json`。

仍需旧桌面对新云端验证、生产环境容量/浏览器/加密上传验收，再准备维护包。真实公司历史应停写后在副本演练。尚未执行公司升级；无公司登录或业务请求。

官方证据：[rc.2发布](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.2)、[累计rc.1说明](https://github.com/deepseek-ai/deepseek-harness/releases/tag/dsh-v0.2.1-rc.1)、[固定源码](https://github.com/deepseek-ai/deepseek-harness/tree/687f282e8625c6b8d11ff58211a90524be2e4317)。本地pin及接口文件hash另见upstream/source-pins.json。
