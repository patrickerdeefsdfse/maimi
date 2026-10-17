# 开发指南

> **2026-10-17 状态更新：** 本仓库已整合 r54/r56/r59/r61/r62 公开应用增量。较早版本叙述用于解释历史，当前状态以 [当前架构与版本](../CURRENT-ARCHITECTURE.md) 和 [组件源码映射](CURRENT-SOURCE-MAP.md) 为准。r63/r64 仅在 experimental，不是已部署能力。根 Compose 不能直接复原当前线上 r62。


本指南面向接手代码、修复问题和增加企业能力的开发者。先读 [README](../README.md) 与 [系统架构](ARCHITECTURE.md)，再选择需要修改的边界。

## 开发前先确定版本

本仓库以原 r52/r53 归档为基础，已整合截至 r62 的公开应用源码；r63 与 r64 位于 experimental，未改变公司部署版本。修复生产问题前，仍应核对当前已部署版本的完整来源清单与配置契约，建立受控分支，不能直接拿归档快照覆盖当前服务。

`SOURCE-PROVENANCE.json` 记录基础源、派生导出和实验增量来源，不替代当前全部 HEAD 文件的校验清单；精确文件版本由 Git 提交追溯。`release-sources` 保留历史公开代码。部分示例已脱敏，部分大运行时制品未收录，历史原发布哈希与归档文件哈希可能不同。旧工具中写死的基线、目录和容器身份不应被删除来“让脚本跑起来”。

## 开发环境

| 范围 | 前提 |
| --- | --- |
| Python 合成测试 | 独立虚拟环境、网关依赖与测试工具；生产文件系统安全测试需 Linux |
| Node 合成测试 | 与运行时资料兼容的 Node.js；所列纯 Node 测试不需要安装完整 DSH |
| 完整云端环境 | Linux 容器能力、受控 DSH/Office 制品、数据库、Redis、独立网络和合成身份 |
| 桌面构建 | Windows 构建机、固定 Electron 与 Node/DSH 制品、办公组件及校验工具 |

依赖依据分别是：`services/gateway/requirements.txt`、`ops/local-env/requirements-test.lock.txt`、`runtime-build/package-lock-linux-x64.json`、`desktop-build/runtime-package-lock.json`、`office-engine/requirements-linux.lock.txt`。

本地测试锁限定的是对应开发环境，不能当作线上全部间接依赖或基础镜像摘要。Linux 与 Windows 的原生依赖不能互换；不要向共享 Python 或正式运行时目录临时安装实验包。

## 最小合成测试入口

在仓库根目录执行，使用公开合成数据，不需要真实员工身份或供应商密钥。

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r services/gateway/requirements.txt -r ops/local-env/requirements-dev.txt
python -m pytest verification/logistics/tests/test_logistics_query.py verification/logistics/tests/test_logistics_mcp_integration.py -q
node --test services/personal-cloud/test-catalog.mjs services/personal-cloud/test-idle-control.mjs
```

Windows PowerShell 创建和激活虚拟环境时使用：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

后续使用同样的 `python -m pip`、`python -m pytest` 和 `node --test` 命令。若公司策略限制脚本激活，可直接调用 `.venv` 中的解释器，不需要修改全局执行策略。

上述 Python 两文件在文档准备时通过 40 项测试，Node 两文件通过 21 项测试。这些结果来自已有依赖的本地环境，不代表全新机器依赖安装、公司网络或全部仓库测试均已通过。

### 按变更选择验证

| 变更 | 最少需要覆盖的行为 |
| --- | --- |
| 登录、模型、客户端会话 | 双账号隔离、续期、撤销、过期、空模型目录、拒绝未授权模型 |
| 云端管理器 | 同员工重复进入、并发冷启动、资源占位、取消、超时、释放与热续接 |
| 文件上传与转换 | 实际浏览器请求、中文文件名、大小变化、收据摘要、取消、错误不自动重放 |
| 企业任务 | 当前会话选择、许可/epoch、文件归属、幂等、取消、结果与成品访问 |
| 原生适配或桌面 | 同版运行时、真实 Host、前端模块、旧数据副本、打包后 ASAR 与完整资源审计 |
| 发布工具 | 基线拒绝、目标范围、备份、阶段中断、失败恢复、并发外部改动拒绝 |

历史测试并非全部与最终归档代码同步：部分仍引用原工作目录下未入库的 `output` 夹具，或保留旧接口签名与 HTML 预期。桌面 `package.json` 有测试入口，但当前归档未收录其完整测试目录。应先区分真实回归、夹具缺失和版本不匹配，再补齐对应发布测试；不要用批量删除断言掩盖问题，也不要把未运行的全量测试标成通过。

## 启动完整开发环境

`dev-local` 提供历史合成环境与模拟公司接口，适合理解协议和隔离验证，但其中含不同日期的迁移记录。新机器启动前需要：

1. 核对 Compose、环境模板、锁文件与所测试源码属于同一版本。
2. 使用独立项目名、合成身份、独立卷和回环入口；不复用生产 `.env`、令牌或业务卷。
3. 补齐所需运行时制品，并确认 DSH、Office 和社区扩展的版本及摘要。
4. 先验证数据库迁移、Redis、网关与合成身份，再验证原生界面和任务链。

本仓库没有经过验收的通用“一条命令启动完整当前生产系统”流程。根 Compose 是部署素材；较早 `dev-local/README.md` 的路径、测试数量、版本和默认值不是当前生产说明。建立新的完整开发栈后，应记录精确输入、命令和恢复方法，再将其纳入团队基线。

## 修改入口

- **网页入口与文件桥：** `services/gateway/app/personal_cloud_web.py`、`personal_cloud.py` 及相关文件模块。
- **个人模型：** `personal_model_proxy.py`、`model_policy.py` 和原生 `catalog.mjs`。
- **云端生命周期：** `services/personal-cloud/manager.py` 与 worker/idle/task 适配。
- **业务项目：** `services/gateway/maimi_projects`；平台许可与文件接口在 `app/project_platform.py`。
- **物流：** `app/logistics_query.py`、`app/kuaidi100_mcp.py` 与原生物流技能覆盖。
- **办公能力：** `office-engine`、`services/office-service` 及原生 Office 桥。
- **桌面外壳：** `desktop-local`；运行时和品牌组装参考 `desktop-build`、`runtime-build`。

以上是当前源码整合位置。文件名相同不等于来源相同；开发和发布仍需核对完整来源清单，experimental 不会自动进入线上运行时。

## 开发企业技能

优先复用 DSH 的原生附件、工具、模型和工作区能力。只有企业身份、供应商访问或稳定业务执行确实需要时，才增加服务端连接器或项目动作。

建议依次实现：能力/参数契约、会话选择、服务端授权、真实文件引用、幂等任务、取消与结果、用户可理解的错误、独立验证。模型可以规划任务，但不能生成自己的员工身份、权限、凭据、任意访问地址或宿主路径。

有文件处理时，区分浏览器上传、安全网关转换、原生存储、办公读取和业务提取。OCR 读到文字不等于已经正确建立指标关系，任务 `completed` 也不代表所有字段完整。测试应包含噪音、别名、多个对象、缺项与歧义，而不只验证最理想输入。

## 构建与依赖升级

当前若干构建脚本保留原构建机绝对路径、缓存和外部输入，例如桌面打包器会寻找固定 Electron 缓存，Linux 组装工具依赖已验证的基础导出。这些资料用于重建链路，不能保证新机器原样执行。

迁移构建机时，先把外部输入和路径改成明确的受控参数，保持版本组及哈希守卫，再验证运行时、前端、原生 RPC、旧数据副本和成品。完整上游安装树不应回填进 Git；应进入受控制品库，并保留依赖锁、来源和许可。

数据库修改新增迁移，不改写已应用迁移以绕过校验。对破坏性模式变化先提供兼容阶段、数据备份和隔离恢复验证。生产数据库不作为开发测试夹具。

## 提交与评审

每个合并请求说明：触发场景、修改后的行为、涉及组件、验证命令与结果、未验证范围、配置/数据影响及恢复办法。提交前检查秘密、业务附件和原始日志没有进入 diff。

当前公司 GitLab 尚无已配置并验收的 CI/CD。推荐后续先把可重复的语法、合成测试、依赖检查和构建审计迁入 Runner，再建立隔离验收和受控发布门禁；不要把未完成的流水线显示成项目已有能力。
