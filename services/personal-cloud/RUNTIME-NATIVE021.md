# r65 管理器候选：原生运行时与历史启用门槛

本目录为本地源码候选，未连接或部署公司服务器。`manager.py` 从当前归档管理器复制后做有限分支改动，旧 0.2.0-rc.2/r41 逻辑保持独立。

## 新运行时配置

选择 `PERSONAL_CLOUD_WORKER_RUNTIME=0.2.1-rc.2` 时必须同时提供：

- `PERSONAL_CLOUD_WORKER_IMAGE`：已核验 Linux/amd64 镜像、且与目标 Docker Engine `/images/<ref>/json` 响应 `Id` 一致的不可变标识，格式 `sha256:<64 hex>`，不能填可变 tag。不同 Docker 存储驱动的顶层索引标识与平台配置标识可能不同，发布控制器应针对目标 Engine 实测，不应要求两个不同层次的摘要相等。
- `PERSONAL_CLOUD_RUNTIME_RELEASE_SHA`：此次已冻结发布清单 SHA-256。
- `PERSONAL_CLOUD_RUNTIME_ACTIVATION_FILE=/opt/personal-cloud/runtime-r65/activation.json`。
- `PERSONAL_CLOUD_RUNTIME_OWNER_RECEIPTS_DIR=/opt/personal-cloud/runtime-r65/owners`。

上述文件应由离线发布控制器生成，以只读方式挂载给管理器，root 拥有且不可由组/其他用户写入。新版拒绝 worker/history/workflow/runtime-followup/FAST_ENTRY 全部旧覆盖层，也拒绝旧 canary 配置。业务代码、原生模块和公司 UI 都必须来自已经核验的新镜像。

全局 activation 的精确字段为：schema=`megmeet-runtime-activation-r65-v1`、runtime=`0.2.1-rc.2`、release_manifest_sha256、image_id、status=`ACTIVE`、history_admission=`OWNER_SNAPSHOT_RECEIPTS_REQUIRED`、legacy_overlays=`false`。旧 r41 activation 不能启用新版。

## 历史与回退

每个 owner 在首次打开新镜像前，都需要 `owners/<owner digest>.json` 的管理员回执。回执 schema=`megmeet-runtime-owner-snapshot-r65-v1`，status=`SNAPSHOT_VERIFIED`，绑定 owner、manager_namespace、source_runtime=`0.2.0-rc.2`、target_runtime=`0.2.1-rc.2`、release_manifest_sha256、image_id，以及 state/files 两个卷各自的 name 和 created_at。backup 对象含 state_sha256、files_sha256、verification_receipt_sha256 三个备份验证摘要。

管理器验证的是可信管理员回执及其范围，不会自行复制、解密、清空或迁移员工数据，也不会把 session v4 或 SQLite v8 相同当作备份成功。没有回执时会返回 `personal_cloud_runtime_snapshot_required`，不创建 worker，不调用原有 `migration-admit`。已存在的命名卷保持原名、原挂载点。

`runtime_snapshot.py` 已提供独立的离线快照函数 `prepare_owner_snapshot`。它要求旧 0.2.0 管理器已经停止、同项目没有其他运行中的管理器、该 owner 的两卷无任何容器引用、卷标签和创建标识吻合、备份目录为空且 root 拥有。控制器自身不停止服务或删除 worker，不自动激活新版。固定摘要的本地 Python helper 无网络、源卷只读、根目录只读、512 MiB / 32 PID / init；生成两个归档后，由第二个 helper 逐项解压校验并计算摘要，再由宿主机复核摘要、复查停机和卷身份，最后独占创建管理员回执。外部/绝对符号链接、特殊文件、超出上限的归档被拒绝。

该函数已在本机用两个新建的合成卷完成真实 Docker 验证：归档内容与源文件相同、源文件摘要未改变、生成收据被真实候选管理器接受，所有测试容器/卷均已清理。**全局发布/新用户接入编排和恢复控制器仍未完成，本候选没有启用生产环境。** 新开通用户的空卷也需纳入后续发布/准入控制器，不会假装已证明空卷。旧管理器分支保留不意味着可以将已由新版写入的卷直接降级；正式回退必须停机并恢复与旧镜像对应的完整 state/files 备份后再启用 r41，不能只切换镜像。

旧 history helper 的运行时转换边界与新版镜像 ENTRYPOINT 尚未完成迁移，因此新版路径明确拒绝其 inventory/delete-session/migration 操作，返回 `personal_cloud_native021_maintenance_unreviewed`。这是一项明确待完成的功能，不是静默启用旧 helper。普通工作区的资源/数据保留策略不变：1 CPU、1.5 GiB 内存、512 PID、init 回收、只读根目录，以及 `/state`、`/workspace` 两个持久卷。

## 本地验证

`test_manager_native021.py` 加载真实候选模块，使用合成配置、临时 root-owned 回执与模拟 Docker 响应，不启动公司业务。8 项测试已在本机 Linux Python 3.12 容器通过：旧/新 activation 隔离、固定镜像、新版拒绝覆盖层、两卷身份/员工/发布绑定、容器配置、拒绝旧维护 helper、无快照的 prepare_open 不创建 worker。

`test_runtime_snapshot.py` 的 10 项单元测试和 1 项真实本机 Docker 集成测试通过。集成测试必须显式设置 `MAIMI_SNAPSHOT_LOCAL_DOCKER_TEST=1` 和 `MAIMI_SNAPSHOT_HELPER_IMAGE=<本地固定镜像摘要>`，以 root 运行；只对测试新建且随机 owner 的两卷执行。既有运行管理器会阻止测试，不会被测试停止。完整结果见本地交付的 `validation.json`。
