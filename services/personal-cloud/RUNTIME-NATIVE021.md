# r65 周一云端准入与恢复候选

2026-10-19。本目录是新的本地候选，旧 `gateway/manager-candidate`、已公开的 `project` 快照和 r59～r63 冻结发布包均未修改。没有公司连接、业务请求、部署或员工数据读取。

## 已实现的接口

### 1. 管理器新员工准入

`manager.py` 是 `patch_manager.py` 从旧候选确定性生成的独立副本。新模块 `runtime_admission.py` 在现有 `prepare_open` 的管理器锁内运行，沿用原接口身份认证、内存准入及租约限制。

新增配置必须精确为：

```text
PERSONAL_CLOUD_RUNTIME_NEW_OWNER_RECEIPTS_DIR=/opt/personal-cloud/runtime-r65/new-owners
```

该专用目录由管理员建立、root 拥有、0700，以可写方式只挂载给可信管理器；旧快照回执和全局 activation 继续只读挂载，worker 不得获得这些挂载。

只有两只命名卷此前都不存在，并且没有旧快照回执、先前新员工回执或中断意图时，才允许新建。先独占写入并 fsync 创建意图，再为两卷写入随机创建标签，检查卷标识、归属及无容器引用。固定 Node helper 无网络，第一次验证空目录并初始化 uid1000/0700，第二次以 uid1000、无额外 capability、只读挂载独立确认；最后复核两卷标识并写入绑定当前镜像与发布的回执。

helper 与 native021 worker 都设置 `VolumeOptions.NoCopy=true`，避免 Docker 从镜像自带 `/state` 目录自动复制内容。原生 worker 本身创建所需目录。

已有卷不因“看起来为空”被当作新员工，不扫描或执行其内容。只有一只旧卷、标签冲突、未知引用、验证失败、部分创建中断，都停止准入并保留意图，不删除员工卷或静默重建。异常中断需离线管理员恢复，不自动猜测。

### 2. 全量快照与全局激活

`runtime_release.prepare_release(docker, plan, root)`：

- 先校验主机 Python >= 3.10，固定 helper 镜像中的 Python >= 3.12 且支持安全 tar 过滤。
- 要求固定网关和旧管理器已经停止，项目内没有其他运行的同类服务。
- 旧 manager 的运行时、工作镜像、发布摘要和 namespace 必须等于计划；旧/新镜像都必须由当前 Engine 解析到指定不可变 ID，OS/架构/版本匹配。
- 枚举该 namespace 下全部旧格式命名卷，必须两卷成对、标签正确且无任何容器引用（包括停止的容器）。不允许手工跳过某个员工。
- 为所有 owner 调用独立快照创建与第二遍校验，并写准备回执。没有 `activation.json`，新运行时保持关闭。

计划 schema 为 `r65-offline-release-plan-v1`，字段：`namespace`、`gateway_id`、`source_manager_id`、`source_image_id`、`source_release_sha`、`target_image_id`、`target_release_sha`、`helper_image_id`。这些值必须来自后续生产盘点和冻结制品，不得套用本机测试镜像。

`runtime_release.activate_release(docker, root, target_manager_id)`：

- 要求网关仍停止；旧 manager 容器仍存在且停止，新 manager 是唯一运行的管理器。
- 核对新 manager 六项运行参数及 namespace，重新验证所有备份摘要和卷身份。
- 真实 `/v1/status` 必须 ready、无 worker、native021 准入关闭，先持久化绑定目标 manager、计划摘要和 nonce 的 activation-intent，再独占创建全局 activation。
- 再探测真实管理器已观察到 activation，并持久化 activated 回执。整个创建、探测、完成记录过程均有异常处理；只撤销同 inode 且内容属于本次的 gate，现存或竞争创建的其他 gate 不被删除。失败保留 intent 和无效 gate 供审查，入口持续关闭。
- 返回 `ACTIVE_INGRESS_STILL_STOPPED`。它不会打开网关，也不自动声称用户业务验收通过。

**外层维护事务必须保留旧网关/管理器容器身份。** 应停止并重命名保留，不能先由 Compose 删除后又要求此控制器 inspect 原 ID。外层还必须暂停入口和其他工作区写入者、停止并移除受管 worker 容器但保留卷。这里没有提供直接生产 apply 命令。

### 3. 恢复保留双边数据

`runtime_release.restore_release(docker, root, stopped_target_manager_id, destination)`：

- 要求入口及目标管理器停止、全部源卷无引用；若曾激活，停止的 manager 必须等于激活回执中的目标。
- 验证原始备份，并撤下当前匹配的 activation。篡改或不匹配的 activation 不被覆盖。
- 将旧快照恢复到新的 `namespace-owner-restore-随机标识-state/files` 卷；永不清空或覆盖新版已经写入的原卷。
- 两次独立 helper 校验归档、目录对象集合、所有权、权限、链接及每个普通文件的内容摘要。
- 生成 root-owned `restore.json`，绑定旧镜像、旧发布摘要和每个恢复卷的创建身份。

该 `restore.json` 确实由当前候选管理器消费，绝非未接通的映射描述。将候选 manager 配为旧 `0.2.0-rc.2` 分支，并只读挂载：

```text
PERSONAL_CLOUD_RESTORE_BINDINGS_FILE=/opt/personal-cloud/runtime-r65/restore.json
```

实际 `prepare_open` 验证恢复卷创建身份、使用恢复卷替换 worker 的 `/state` 与 `/workspace` Mounts，不再创建原名卷或执行旧迁移去触碰新版源卷。已有快照覆盖不到的“升级后新建员工”在旧运行时拒绝准入，其新版卷保持原样；不能声称所有新旧用户均可无损自动降级。恢复模式暂时拒绝旧存储维护接口，避免历史清理误删除保留源卷。恢复接口不自动启动旧 manager 或打开入口。

## 运行与安全边界

- 生产运行主机须 Linux/Python >=3.10。宿主机摘要使用流式 SHA-256，不依赖 Python3.11 的 `hashlib.file_digest`。快照/恢复 helper 固定镜像内需要 Python>=3.12；前置检查先于发布目录的持久写入。
- Docker 镜像多平台 index ID 与平台 config digest 可能不同，校验与 manager 的 Engine `/images/<ref>/json` 保持同层含义，不要求不同层摘要相等。
- 快照和恢复统一所有权、权限、成员类型及链接校验；不支持的 uid/gid 或特殊权限在生成有效备份回执前拒绝。归档关闭压缩流后同步文件及目录，恢复先同步数据再同步目录；同步失败不能返回成功。所有卷挂载均禁用 Docker copy-up。
- 备份只读源卷 helper 需 `DAC_OVERRIDE` 读取 uid1000/0700 数据目录；源卷始终只读。恢复写入只对随机新建目标卷，校验阶段只读。helper 都无网络，不执行归档内代码。
- 管理器仍是单实例，不新增多活能力。新员工创建由 manager 现有锁串行；发布/恢复由 root-owned 目录文件锁串行。管理员之外不得调用这些 API、修改回执或访问 Docker socket。
- 未完成/失败的准备或恢复不自动重试，保留归档、意图和卷供检查。不能把部分准备误标 ACTIVE；手动清理或续传恢复须另行编排。
- 快照为当前实现支持的常规目录/文件/内部链接；外部链接、设备文件、超限内容会拒绝。恢复仅接受 uid/gid 0 或1000且无 setuid/setgid/sticky 文件权限。没有宣称保留未采集的扩展 ACL/xattr。

## 本地证据与待整合项

`test_admission.py`、`test_release.py` 为实际候选管理器与接口的本地合成测试。`test_real_docker.py` 仅在显式本机测试开关下创建随机 namespace、合成卷、合成凭据及隔离网络；真实测试覆盖 uid1000/0700 旧卷备份、真实管理器全局 activation、新空卷证明、升级后写入保留、恢复旧内容和真实 manager Mounts 选择。临时容器、网络和卷均按本任务创建身份清理。

仍须由总发布完成：生产当前镜像/配置最小盘点；备份可用容量及空间保留检查；封装镜像和 manager 部署挂载；网关升级与入口切换/健康检查；现场 IAM、加密文件上传和业务验收。native021 的历史库存/删除/迁移维护接口仍保持门禁，整工作区删除也在任何 Docker 操作前拒绝，以免删卷后留下旧准入回执，没有在本目录中冒充迁移完成。当前成果是已测试的编排接口候选，不是生产一键升级包。
