# 原生 DSH Web 一次性验收

状态：脚本已准备；Linux 执行结果待回传。此检查不更新现有服务。

## 为什么使用现有镜像

已验收 Gateway 镜像安装 SDK/runtime `0.1.5rc2`，runtime 提供 `dsh` 命令、完整 `web` profile 和前端资源。使用镜像的不可变 ID，可绕开源码工作区的 pnpm 依赖重装。相关能力见 [rc2 runtime README](https://raw.githubusercontent.com/deepseek-ai/deepseek-harness/dsh-v0.1.5-rc.2/python/sdk-runtime/README.md)，用户此前也回传过该段本地文档。

## Linux 执行

仓库中的两个源文件需放在同一目录：

```bash
bash ops/run-dsh-native-web-probe.sh
```

交付给用户的是同内容的单文件 `dsh-native-web-probe-20260912.sh`。上传到 `/data` 后执行：

```bash
bash /data/dsh-native-web-probe-20260912.sh
```

严格退出选项只作用于脚本的子 Bash，不给交互式 SSH shell 设置 `set -e`。如果本次登录已经启用了它，运行前执行 `set +e`。无需解压、安装依赖或复制 API key。

## 边界和检查

- 读取 `megmeet-ai-new-r10-gateway-1` 的 `.Image` 标量；不读取完整配置。
- 启动新的临时容器，`--network none`，无端口发布、无挂载、无密钥；文件系统只读，临时工作目录位于独立 tmpfs。
- 限制 1 CPU、1536 MiB 内存、256 进程；临时文件最多 512 MiB。内部检查约 85 秒截止，外部约 100 秒截止并清理容器。
- 子进程使用环境变量白名单，独立 HOME/DSH_HOME/缓存/工作目录。临时 token 只用于本机 Cookie 交换，不输出原始日志或 Cookie。
- 检查版本、原生 `web` profile 启动、首页未认证拒绝、token 换取 HttpOnly 且 host-only Cookie、启动图含原生聊天模块、静态与插件资源 HEAD、`/api` 未认证拒绝、WebSocket 未认证拒绝/认证后 101、错误 Origin 拒绝。

成功标记：`NATIVE_WEB_HOST_PROBE: PASS` 和 `PROBE_EXIT_CODE: 0`。失败会显示检查阶段与有限错误代码，把整个输出回传即可。脚本只读资源和握手；没有发送 Agent 任务或 WebSocket RPC 帧。

## 当前失败的修复

本次结果显示 `ERR_DLOPEN_FAILED` 与 `MISSING_PACKAGES: @deepseek-ai/dsh-session-title-llm`。这是 rc2 单文件 runtime 的打包闭包缺口：`dsh` 位于 `apps/cli`，旧闭包检查没有沿应用包继续扫描，因此遗漏了这个由 web 基础 profile 间接需要的 workspace peer。上游已记录同一问题及验证过的 workaround：在 `python/sdk-runtime/package.json` 增加 `"@deepseek-ai/dsh-session-title-llm": "workspace:^"`，更新 lockfile，再完整重建 executable/wheel。参见 [上游问题记录](https://github.com/deepseek-ai/deepseek-harness/discussions/5627)。

不要只复制一个源码包或修改运行中的容器。修复后必须重新生成 runtime wheel、替换测试 Gateway 的 wheel 并重建隔离镜像，再重新运行本探针；每次只以实际输出判断。

已准备一键修复脚本 `dsh-runtime-web-closure-fix-20260912.sh`。它会备份 runtime manifest/lockfile，在 DSH checkout 中执行闭包修复和完整 runtime 构建，替换隔离 Gateway 的旧 runtime wheel，重建并重启该测试 Gateway，最后自动重跑本探针。默认路径均为当前已确认的测试目录；脚本不会删除旧系统。

## 结果不能代表什么

资源 HEAD 成功不能证明浏览器执行 JavaScript 成功；WebSocket 101 不能证明远程调用、事件流或完整对话成功。该探针不验证 SSO、模型、项目路由、品牌、数据库历史、保留清理策略或多用户隔离。下一阶段需要真实浏览器与平台能力集成验收。现有 React 验证页和旧系统都保留。
