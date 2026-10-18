# r65：最新版 DSH 沙箱接口与 r64 接入边界

证据：官方 `dsh-v0.2.1-rc.2` / `687f282e8625c6b8d11ff58211a90524be2e4317`，本地只读源码 `../upstream/source/deepseek-harness-687f282e8625c6b8d11ff58211a90524be2e4317/`。此文件描述接入契约，不代表任务池已在公司启用。

## 决策

保留新版 DSH 原生的命令工具、文件工具、PTY、取消、审批、PTC 和会话资源管理。企业任务容器放在这些工具使用的执行后端下面，不重写一套聊天工具协议。r64 不能只代理 shell：必须让 `fs`、`subprocess`、`shell`、`sandbox` 和 PTC 共享同一容器文件与进程环境。

上游原生 Sandbox 是同一执行环境内的文件访问约束，明确不是跨员工容器隔离。现有用户工作区负责会话常驻状态；独立任务容器承担资源限额、任务依赖、出网范围、生命周期与产物移交。两层并存。当前只有一台公司服务器，任务池先同机隔离运行，保留以后迁到独立节点的接口；不可把“原生 Sandbox”说成独立机器。

## 必须实现的新版协议

| 服务 | 最新契约 | r64 的必要适配 |
|---|---|---|
| `ctx.fs` | `FileSystem.resolve(path,{cwd,signal}) -> Promise<FsTarget>`；`processPath(target)`、`fileUrl`、`contains` 使用同一执行环境的规范路径；`stat/lstat/readText/streamText/readBytes/readByteRange/listDir`，原子文本写入和版本前置条件 | 路径只能映射到任务租约目录；保留字节上限、取消、版本冲突与写入意图。不能让 `processPath` 返回宿主临时路径，却让进程在容器内运行。宿主路径映射可明确不支持。 |
| `ctx.subprocess` | `resolveExecutable(command,env,signal)` 同环境查找；`terminalEnvironment`；`spawn(spec)` **同步返回 handle**；`spawnTerminal(spec)` 异步返回真实 terminal handle | 先返回本地协议 handle，再由内部任务完成远程连接/启动；不能把一次 HTTP 命令结果冒充 handle。保留 stdin/stdout/stderr 流、结束状态、超时/取消与输出偏移读取。 |
| `SubprocessHandle` | `done` Promise、`terminate()`、`waitForExit(signal)`；非消耗式 collected readers；可有 `controlDuplex` | 必须传递双向控制通道，PTC 借此调用 host bindings；普通 stdout 不能混入控制消息。外部网络断开要区分已提交但未知结果，不自动重放有副作用的命令。 |
| `SubprocessTerminalHandle` | 真 PTY 的 `write/resize/foreground/activity/signals`，输出流与 `done`，异步等待的 terminate | 不能用普通 pipe 伪装终端；不支持时明确拒绝，不能静默退回宿主终端。 |
| `ctx.shell` | `ShellExecutor.resolve(request) -> ShellExecSpec`；`execute(spec) -> Promise<ShellExecution>`；执行结果、增量输出和 close 生命周期分离 | 保留新 Bash/PowerShell 工具的 prepare/execute/collect 流程；非零退出、超时、用户取消是结果类别，启动基础设施错误才 reject；进程由 subprocess 拥有，不能在 shell 插件重载时随意遗弃或杀掉。 |
| `ctx.sandbox` | `confine(argv,policy,signal) -> Promise<ConfinedArgv>`；mode 为 read-only/workspace-write/danger-full-access；enforcement 标明 full/partial | 为任务容器里的实际命令提供访问约束；无可用隔离器时 `SANDBOX_UNAVAILABLE`，禁止自动无约束执行。资源容器本身不等于 workspace-write 文件策略已实施。 |
| `ctx.ptcRuntime` | `resolve(PtcRunRequest)` + `run(spec)`；language/isolation/programFormat、timeout、executionInstructions、release(owner) | Node/Python PTC 的程序和工具绑定必须在同一个文件/进程环境；跟踪每个 Agent 的 owner 与取消。不要把 PTC 当一段没有 host callbacks 的普通脚本。 |

直接源码位置：`packages/fs/fs/src/index.ts`（resolve 132、processPath 142、contains 173、readBytes 228）；`packages/subprocess/subprocess/src/index.ts`（runtime 117、resolveExecutable 133、terminalEnvironment 144、spawn 153、spawnTerminal 162）；`packages/shell/shell/src/index.ts`（resolve 84、execute 93）；`packages/sandbox/sandbox/src/index.ts`（mode 30、confine 177）；`packages/ptc-runtime/ptc-runtime/src/index.ts`。

## PTC 与进程回收

新版 Node PTC 依赖 `fs/subprocess/sandbox/sandboxPolicy`，默认每次调用隔离，可配置按 Agent 保留内存。保留内存不等于持久存储：取消、超时、权限变更、Agent dispose 均会清理。默认调用超时 120 秒、最大 600 秒、输出上限 64 MiB、V8 heap 512 MiB；这些是该运行时的默认值，不是公司批准的容量承诺。

Python PTC 为实验组件，当前针对 Linux/macOS，也要求 sandbox 与 sandboxPolicy；必须先 resolve 再 run，不再提供“没有 sandbox 就裸跑”的兼容方式。公司网页无需为了 PTC 把宿主 Python 暴露给员工。

此前工作区与 Office 的故障是 PID 1 没有回收僵尸进程，单纯增大 pids 只会延后复发。任务容器继续设置 `init=true`、任务结束等待进程树退出、期限后的 TERM/KILL、幂等租约回收；同时给 PIDs、内存、CPU、磁盘、输出和任务时长限额。r61 已证实回收改善，升级不能撤掉 init。

## 原生 SSH 不可直接代替任务池或 service

`packages/ssh/ssh/src/index.ts` 使用运维预先配置的 OpenSSH alias、绝对路径 helper 和 SHA256 校验，POSIX 客户端限定；helper 支持 node-script/executable 两种载体。它可为同一执行环境提供远端协议，但不是网页任意 IP+密码登录表单，也不是每任务容器调度器。用户要求的 service 连接区仍由公司插件和独立 broker 完成，默认 root、端口 22，密码不能写入聊天或模型上下文。

## 验收范围与失败规则

1. 先用全新合成租约检验读、写、路径越界、二进制附件、shell 输出流、PTY、取消、超时和会话退出回收；不得用公司真实文件做首次适配实验。
2. Node PTC 验证工具回调和双向控制通道；适配未完成时不宣称已实现原生 PTC。
3. 并发租约之间文件、进程、网络身份与配额必须独立；单机总容量不能用“每个用户最大内存 × 开通账号数”直接预分配。
4. 所有 exec 都由任务身份决定目标，不能由模型输入绕过租约选择宿主路径、Docker socket、宿主 PID 或网络命名空间。
5. 任务创建/连接错误不应无限自动重试；不明确远程副作用是否已执行时，报告结果未知并查询任务状态。
6. 官方原生接口的版本固定在 0.2.1-rc.2；原 r64 若按旧版 0.2.0 组装，不进入本次正式 runtime 默认 profile。先保持 disabled，完成上述契约测试后再启用。

官方说明：[Sandbox](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2/packages/sandbox/sandbox)、[Subprocess](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2/packages/subprocess/subprocess)、[FS](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2/packages/fs/fs)、[PTC](https://github.com/deepseek-ai/deepseek-harness/tree/dsh-v0.2.1-rc.2/packages/ptc-runtime)。
