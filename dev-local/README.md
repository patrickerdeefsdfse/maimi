# 本地 Linux 容器开发环境

## 2026-09-17 新IAM协议已在本机启用

当前运行配置明确使用`AUTH_MODE=iam_direct`，依据新1.docx，不再用旧query-user登录。合成IAM授权码→真实Gateway/Redis会话→账号目录关联工号→模拟聊天/真实PostgreSQL历史→退出撤销/重登已通过双账号完整验收。目录worker每120秒全量同步，快照超过300秒拒绝认证。Gateway只读目录卷，签名secret仅目录worker读取。

执行`dev.ps1 smoke`现在运行`iam_smoke.py`。`rebuild-iam.sh`构建并启动本机IAM/Gateway相关服务；下方早期query-user/Compose smoke说明是历史，不能作新IAM验收依据。真实公司参数/个人TokenKeyAPI/桌面系统浏览器交接尚缺，见`../docs/IAM-DIRECT.md`。原生桌面仍单本机开发用户，两条链尚未合流。


2026-09-17补充：Milvus/Elasticsearch 已有独立按需开发配置和验证流程，见 `retrieval/README.md`；桌面仍使用原生DSH，检索服务不向客户端发布数据库端口。实际安装/数据持久化结果以接续记录为准。

这是独立的本地开发栈，不引用生产 `compose.yaml`、`.env`、公司 secret 或服务器数据。只有边缘 Nginx 在本机 `127.0.0.1:28090` 提供 Gateway 代理；Gateway、PostgreSQL、Redis 和模拟公司接口没有宿主机端口，均只在 `internal: true` 的内部网络运行。边缘 Nginx 另接一个普通桥接网络以发布回环端口，只允许代理固定的 `gateway:8080`，以非 root 用户、只读文件系统和临时目录运行。

本地使用真实 Gateway 源码、全部现有 SQL 迁移、PostgreSQL 16、Redis 7、Python 3.12，以及现有 Gateway 的直接依赖固定版本。SSO 和模型回答为明确的合成数据。第一次拉镜像和安装构建依赖需要访问公共软件源；应用依赖连接均指向本地内部网络，边缘 Nginx 没有外部上游。

## 2026-09-17 实际验证记录

独立 WSL2 发行版 `MegmeetDev` 已安装 Ubuntu 24.04.5。主线程在该 Linux 环境实际运行了以下测试，全部通过：

| 范围 | 通过数 | 验证边界 |
| --- | ---: | --- |
| dev-local 模拟接口与依赖锁 | 9 | 本地夹具与构建输入选择 |
| Gateway | 11 | 现有 Gateway 自动化测试 |
| native-entry | 189 | 入口、启动器及协议的现有本地测试 |
| Node 续期控制器/浏览器适配 | 52 | 合成连接和浏览器能力测试 |
| 合计 | 261 | 不等同生产或完整原生 DSH 验收 |

主线程已实际完成首次镜像构建、Compose 服务启动和容器链路 smoke，均通过。当前 Docker Engine 不为只连接 internal 网络的 Gateway 发布宿主机端口，已通过最小边缘 Nginx 解决，应用与数据库仍保留内部网络。此后实际验证结果为：

- 边缘代理启动、WSL/Windows 回环访问及 **Windows 端完整链路 smoke：PASS**。
- 镜像 digest 和完整 Python 依赖锁捕获：PASS；锁定后重建输出 `LOCAL_EXACT_LOCK`，`pip check`：PASS。
- 本地停止、终止 WSL、重新启动，以 `--no-build --pull never` 使用本地镜像恢复：PASS。
- 恢复后两条合成历史仍保留，再次完整 smoke：PASS。

WSL 包装器已使用受管的后台保活进程，避免最后一个 shell 退出后环境提前停止；停止操作只清理自己的进程。以上是真实本地 Linux/容器验证，**定制原生 DSH、真实公司 SSO、真实模型调用、个人 TokenKey 接口和与生产镜像逐字节一致性仍未验证**。

后续 Codex 可从 Windows 直接调用已有包装器，无需用户往返粘贴 Linux 命令：

```powershell
& 'D:\麦格米特\linux\new-system\ops\local-env\dev.ps1' -Action status
& 'D:\麦格米特\linux\new-system\ops\local-env\dev.ps1' -Action test
```

它固定进入 `MegmeetDev` 的 `megmeet` 用户，转到本地 `dev-local`；`up`/`smoke`/`lock`/`restart`/`stop` 分别负责本地启动、链路验证、捕获版本、重启 Gateway 和停止本地服务。`shell` 打开 Linux 开发终端。生产服务器仍只通过用户现有堡垒机流程交付，不由该包装器连接。

## 启动和验证

在本目录执行。每条 Compose 命令显式指定本地环境文件，防止自动加载生产 `.env`。

```bash
docker pull postgres:16
docker pull redis:7
docker pull python:3.12-slim
docker pull nginx:alpine
docker compose -p megmeet-dev-local --env-file defaults.env -f compose.yaml up -d --build --wait
docker compose -p megmeet-dev-local --env-file defaults.env -f compose.yaml --profile test run --rm smoke
```

最后一条验证：真实数据库迁移/Redis 就绪、未登录拒绝、两个合成账号鉴权、Gateway→模拟模型回复、两人使用相同客户端会话 ID 时各自历史分离。打印 `NATIVE_DSH_COMPANY_SSO_REAL_MODEL: NOT_TESTED`，避免把合成验证当作现场验收。

本地只读健康地址：`http://127.0.0.1:28090/health` 和 `/ready`。没有新增或替代 DSH 前端。

源码以只读绑定挂载进入容器，可在电脑编辑源码后仅重启对应服务：

```bash
docker compose -p megmeet-dev-local --env-file defaults.env -f compose.yaml restart gateway
```

新增迁移后先运行 `docker compose -p megmeet-dev-local --env-file defaults.env -f compose.yaml run --rm db-migrate`。停止使用 `docker compose -p megmeet-dev-local --env-file defaults.env -f compose.yaml down`，不附加 `--volumes`，合成历史数据会保留。

## 固定版本与当前缺口

生产证据只有 PostgreSQL `16`、Redis `7`、Python `3.12` 主版本/系列，以及 Gateway 直接依赖版本。生产镜像的完整可分发构件、底层 digest 和全部间接依赖仍未取得，因此不能宣称本地与生产逐字节相同。

首次成功构建后执行：

```bash
python capture_versions.py
```

它先将运行中 Gateway/Mock/PostgreSQL/Redis/Nginx 的镜像引用与待捕获标签都解析为 `linux/amd64` 平台镜像 ID 并比较，真实平台内容不同就停止。Docker 29/containerd 的 attestation 或多平台索引 digest 改变、但实际平台镜像相同时不会误报。清单分别记录容器原始镜像引用、运行平台 ID、标签描述符 ID、标签平台 ID，以及基础镜像 digest、实际 Python 包版本及 Gateway/迁移源码 SHA256；不读取任何 `.env`、容器环境或业务表。生成的 `locks/images.env` 用于后续 Compose 命令替代 `defaults.env`。请在首次构建前就拉取上述基础镜像，避免捕获前重新拉取不同版本的标签。

首次没有锁时，Dockerfile 按 Gateway 原 requirements 构建；捕获完成后再次构建会**自动使用完整的 `requirements.installed.txt` 精确版本锁**，包括间接依赖，以 `--no-deps` 禁止解析出新版本，再执行 `pip check`。构建前还校验锁自身 SHA256、原 requirements SHA256，以及 Linux/CPU 架构/Python 次版本；不一致立即停止，不静默回退。此锁固定的是**本次本地环境**，不是尚未取得的服务器真实间接依赖。

```bash
docker compose -p megmeet-dev-local --env-file locks/images.env -f compose.yaml build gateway db-migrate
docker compose -p megmeet-dev-local --env-file locks/images.env -f compose.yaml up -d --wait
docker compose -p megmeet-dev-local --env-file locks/images.env -f compose.yaml --profile test run --rm smoke
```

需要有意升级直接依赖时，先保存 `locks` 历史，再显式移走 `requirements.installed.txt` 和 `runtime-manifest.json`，重新进行首次构建、捕获、锁定重建及测试。不要只改锁文件内容绕过 SHA 校验。

现网定制原生 DSH 版本为 `0.1.5rc2`，个人 Host 镜像证据 ID 为 `sha256:92df7271a90079aa38ce0d3f98f97cf088d7472f2bcd5e331f802bb4af4f8236`。本地 `vendor-wheels` 只有占位文件，没有该定制镜像或完整 DSH 源码，Gateway的SDK开关仍为 `DSH_ENABLED=false`；按用户后续明确授权，已独立增加官方0.1.6-alpha.1原生DSH容器及桌面入口，详情见 `dsh/README.md`。这没有替换现网定制镜像，也尚未接真实公司个人Host路由。

当前模拟接口不实现续期、退出、SSO 重定向或个人 TokenKey 领取，避免虚构公司的接口契约。它只提供 Gateway 已确认的 query-user 和 OpenAI-compatible 非流式和SSE流式聊天契约（用于原生DSH）；原生登录生命周期的现有候选测试仍在原测试目录执行。

所有 `local-synthetic-*` 口令和 `LOCAL-FIXTURE-*` 身份均为公开测试夹具，不使用任何真实员工资料，禁止部署到公司服务器。

## 本地检查

```bash
python -m unittest -v test_mock_company.py test_dependency_lock.py
```

该测试验证模拟接口本身，不替代容器启动和完整链路 smoke 验证。
