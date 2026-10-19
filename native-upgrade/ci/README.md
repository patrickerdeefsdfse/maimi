# 原生升级候选的持续检查

这两条工作流用于开发分支，不部署公司，不读取员工数据，不需要公司账号、私钥或服务密钥。定义加入仓库后，须检查对应提交的 GitHub 运行结果；不能将本地测试或旧提交的成功状态当成本次远端通过。

## Linux：契约、准入和恢复

`.github/workflows/native-dsh-candidate.yml` 在相关 PR 变化或手动触发时运行。原生 API、工作区配置、请求类型和打包检查沿用现有任务。

桌面回归在安装锁定依赖、下载并校验官方源码后，显式运行设备登录、子进程环境边界、原生会话续登、启动异常清理、桌面外壳、退出和 staging 七个测试文件。凭据均为测试值；不连接公司 IAM。`desktop/tests/enterprise/` 中的实际 Host/界面测试和独立服务端测试已有本地验证，完整运行方式见该目录说明，不将这些本地结果视为远端 CI 已执行。

新增云端任务先运行 `check-activation-sources.py`，确认实际 manager 镜像构建的四个模块与测试目录一致。Python 3.12 虚拟环境安装固定 FastAPI/HTTPX 直接依赖，随后以 root 运行 `test_admission`、`test_release`、`test_snapshot_guards`，目前共 39 项。这是为了真实执行 root 所有权和 0700/0600 回执检查，不把它们替换成宽松 mock。Docker API 响应仍使用合成替身；不会连接 daemon。

在隔离的 Linux 开发环境中，可使用相同的 Python 环境运行：

```bash
python native-upgrade/ci/check-activation-sources.py
cd native-upgrade/gateway/activation
# 使用装有 fastapi==0.115.6、httpx==0.28.1 的 Python 3.12；
# 以下命令仅在一次性测试容器或隔离测试机的 root 环境执行。
python -B -m unittest test_admission test_release test_snapshot_guards -v
```

测试覆盖空卷证明、旧卷拒绝、恢复映射、激活意图、现有 gate 保护，以及文件和目录同步失败。实际 Docker 的镜像/卷/挂载行为另由 `gateway/activation/test_real_docker.py` 在显式本机合成环境验证；当前 CI 没有自动执行该集成测试，也不代表它已经在生产环境演练。

## Linux / Windows：监控元数据独立回归

`native-dsh-candidate.yml` 中的 `observability-metadata` job 使用 Ubuntu latest / Windows 2025 矩阵和 Python 3.12，各自有 10 分钟上限。它只安装 `requirements-monitor-tests.txt` 中的固定直接测试依赖，并运行：

```sh
python -m pip install -r native-upgrade/ci/requirements-monitor-tests.txt
python -B -m pytest -q -p no:cacheprovider services/gateway/tests/observability
```

目前为 79 项：48 项后台、适配及生命周期检查，31 项实际业务计量检查。没有公司请求、真实 Docker、Office 转换或部署；不依赖桌面编译，不安装 Office 绘图依赖，不执行其他旧网关测试。测试依赖清单仅固定直接依赖，不能代替生产依赖锁或供应链审计。完整覆盖与未覆盖项见 [运行监控说明](../../docs/DEVELOPER-MONITORING.md)。

## Windows：同仓 PR 或手动编译候选

`.github/workflows/native-dsh-windows-candidate.yml` 在同仓 PR 涉及桌面或本工作流变化时运行，也支持手动触发。job 检查 PR 的 head 仓库与当前仓库相同，不运行其他仓库的 PR；没有 `pull_request_target`。使用 Windows 2025、Node 24、Python 3.12、pnpm 11.28.5。固定官方提交为 `687f282e8625c6b8d11ff58211a90524be2e4317`，归档 SHA-256 为 `157903b22c2881d3678d846755afe957a48df834e747deda805a1195ce25a146`。下载器校验归档，装配器校验完整源码锁，然后仅在新建目录应用公司外壳修改。

执行原生外壳 staging 测试后，使用锁定依赖编译官方 artifacts 和桌面 shell；保留源码变更回执供检查。工作流不启动 Electron，不进行真实登录，不生成安装器，不签名、不更新、不发布程序。Office payload、系统凭据存储实机、企业文件桥及正式安装器验收仍是独立门禁。
