# r64 runtime provider：本地验证边界

当前状态：**本地候选，默认关闭，禁止作为已就绪节点接入员工任务**。SDK 与策略的合成测试通过，不等于真实 gVisor 沙箱已经启动或公司部署完成。此目录没有公司凭据、员工任务数据或公司执行记录。

## 本轮已修正

- OpenSandbox Python SDK 1.1.1 返回的 `SandboxInfo.image` 使用 `image.image`，不能读取 `image.uri`；后者只是 HTTP JSON 字段名。
- `Sandbox.connect` 使用 `connect_timeout`，与 `Sandbox.create` 的 `ready_timeout` 不同。连接等候限 5 秒，创建就绪等候限 45 秒；管理连接内部生命周期限 90 秒，命令连接内部限命令时间加 45 秒，各客户端清理限 5 秒。前置节点检查和最终清理另有时限，不把 90 秒表述为所有外层操作的总耗时。
- 明确禁止 HTTP 自动重试。创建响应丢失后保留不确定状态，不能再次创建；删除响应丢失后不能再次删除或释放预算。SDK 在时限内只读轮询就绪状态，并非重放业务操作。
- 管理端点只允许本机回环，严格布尔启用开关和整型资源额度。服务端策略额外核对 API 绑定地址为 `127.0.0.1`。
- 使用真实安装的 SDK/server 1.1.1 执行合成传输和原始策略钩子，校验上传、执行事件、镜像字段、来源固定、未知结果处理及文件导出恢复。

## 启用阻断项

1. **运行时注入与只读根文件系统冲突尚未解决。** 上游 Docker 路径在创建未启动容器后，调用 `_prepare_sandbox_runtime`，通过 `put_archive` 写入运行时，最后才启动容器。当前策略要求只读根文件系统；同时 `/opt/opensandbox` tmpfs 会遮蔽镜像预装的同名文件。只在镜像里加入 execd、继续沿用现有启动注入和 tmpfs 配置，不能证明可启动。
2. 尚无经过构建、哈希核验和真实运行验收的不可变预装镜像，也未实测 runsc、Docker、内核组合。`deployment.example.toml` 的官方运行时镜像仍为必填占位符。
3. 当前节点策略/防火墙/导出回收检查只有合成数据测试。真实节点隔离、跨容器通信拒绝、宿主访问拒绝、写满限额、子进程回收、超时删除、暂停导出及清理都未验收。
4. 同宿主部署的总量准入、容量保留、DSH 工具和身份受限文件交接由平台层另行完成；不能用测试中的恒真回调替代生产验收。

`enabled` 必须保持 false，真实节点验收回调保持失败关闭；不会回退到 runc、可写根文件系统、host 网络、特权容器或员工工作区 Docker socket。当前代码默认不会启动任何服务。

## 本地测试复现

在独立 Linux Python 3.12 环境中使用精确版本 `opensandbox==1.1.1`、`opensandbox-server==1.1.1`，并保留本目录 `upstream-pins.json`，可分别执行 `python -B test_sdk_contract.py`、`python -B test_node_policy.py`。测试只使用合成传输及合成节点数据，不需要 Docker socket，不连接公司或目标服务器。部署构建还需锁定和审核全部传递依赖，当前本地环境测试不是完整供应链锁文件。

`run_validation.py` 用于生成本次完整本地回执：除上述依赖外，还要求已将 `upstream-pins.json` 指定且哈希核验过的上游源码置于 `upstream/source`，用于记录启动设计的源码证据；它不会自动下载源码。GitHub 可以不包含上游完整源码，单独运行两份测试无需该源码目录。

## 下一步采用预装运行时镜像

具体构建、钩子修改及验收顺序见 [PREINSTALLED-RUNTIME-DESIGN.md](PREINSTALLED-RUNTIME-DESIGN.md)。该文档是待实施设计，尚未生成或验证可部署镜像。
