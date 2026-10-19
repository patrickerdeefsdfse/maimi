# 麦米专属运行监控

这是随 DSH 升级维护的**源码候选，默认关闭，尚未在公司部署**。代码进入 GitHub 或 CI 通过，都不代表公司已安装采集器、启用统计或更新员工客户端；线上状态以公司维护回执和实际验收为准。

监控启用后的页面路径为 `/maimi/developer/monitor`。只有 M00096389、M00000246、M00091350、M00063756、M00003260、M00057343 可读；这些权限不授予 SSH、service 或管理写操作。页面、静态资源和每个 API 请求都重新核验实际 IAM 身份与白名单，浏览器自报工号不能授权。会话失效或撤权时清空页面中的用户用量。

## 组成及源码入口

| 层 | 仓库路径 | 数据与边界 |
| --- | --- | --- |
| 宿主采集 | [tools/monitor-collector](../tools/monitor-collector/) | 固定 Docker 元数据投影、cgroup v2、受限目录元数据计量；不进入容器执行，不读取环境变量、聊天、文件正文或原始日志 |
| 私有传递与身份归属 | [services/gateway/app/observability](../services/gateway/app/observability/) | 读取有界私有快照，核验 IAM 和数据库工作区归属；网关不获取 Docker socket，容器标签不能直接决定员工工号 |
| 实际业务计量 | [usage_producer.py](../services/gateway/app/usage_producer.py) | 接入公司模型代理、远程 Office 终态、已提交企业任务终态；不以模型自述、轮询次数或前端请求冒充实际用量 |
| 存储与聚合 | [monitor.py](../services/gateway/app/observability/monitor.py) | 私有 SQLite 元数据库、7 天保留、最多 10 万事件、5 秒聚合缓存；固定字段，不保留提示词、回复、命令、文件名和凭据 |
| 监控界面 | [assets](../services/gateway/app/observability/assets/) | 同源只读页面，30 秒刷新，页面隐藏时暂停；区分过期、未采集和统计覆盖 |
| 部署候选 | [deploy/monitor-candidate](../deploy/monitor-candidate/) | systemd 与 Compose 接线示例；必须核验宿主权限、目录、挂载和实际镜像，不能直接视为生产配置 |

## 指标应当怎样理解

CPU 以实际核心使用量表示；内存使用与限额分开；进程限制包含线程。存储是目录元数据得到的逻辑体积估算，不是备份验收或物理磁盘账单。IO 是内核块设备计数，网络属于网络命名空间，不能把共享服务简单按员工分摊。累计计数和由相邻有效样本计算的速率分别显示；缺样本或重启导致不可比时显示未知。

Office 的资源作为共享服务展示，作业数通过可信业务上下文归属。Office 观察耗时包含队列和轮询，并非引擎纯运行时间。公司模型代理仅记录供应商真实返回的 token；缺 usage 或缓存写入数时显示未知，不推算为零，也不把缓存未命中解释成缓存写入。中断、取消、失败与无法确认结果的请求分别计数，不将它们记成成功。

企业任务统计覆盖已观察并提交的终态，不代表完整任务调度队列。重复观察不会重复计数，队列丢失、存储失败和采集过期会影响窗口完整性。来源在启用后才开始计量，重启前、来源未接入或被保留上限淘汰的数据不能宣称完整覆盖。

**尚未覆盖：**桌面本机任务、本地 Office、绕过此公司代理的模型调用、企业任务崩溃 reaper 补记，以及未来集中任务池的排队位置、任务级峰值、租约和回收耗时。网页上的未知值不是零用量。

## 与新任务沙箱的关系

本采集器面向当前个人云端工作区与共享 Office；这不代表集中任务池已经启用。集中任务池需要独立的执行接口、调度和隔离验收，目标是保留个人持久数据，按任务进行排队与资源准入，并区分 Office、代码和浏览器执行环境。桌面继续本地优先。单机上的逻辑分组也不等于已部署多节点集群。

## 本地回归与 CI

从仓库根目录，在独立 Python 3.12 环境中执行：

```sh
python -m pip install -r native-upgrade/ci/requirements-monitor-tests.txt
python -B -m pytest -q -p no:cacheprovider services/gateway/tests/observability
```

[监控回归目录](../services/gateway/tests/observability/) 当前共 79 项：48 项后台、采集适配和生命周期检查，31 项业务计量检查。测试导入实际网关模块，使用合成 HTTP、身份、数据库回复、时钟和资源样本，不需要公司账号、服务器、Docker daemon、业务文件或模型额度。测试覆盖实际 IAM Cookie 验证与撤销、元数据白名单、未知和过期样本、来源缺失、可信 token 来源、流取消、首次终态去重和非阻塞聚合。

[native-dsh-candidate.yml](../.github/workflows/native-dsh-candidate.yml) 将这组测试作为独立 Linux / Windows job 执行，不等待桌面完整编译，也不扩大到全部旧网关测试。发布后应查看**对应提交**的 Actions 结果；本地通过不等于远端已通过。

这 79 项不覆盖宿主 Linux 采集器的实际部署、公司 IAM 连通、真实 Office 转换、浏览器视觉渲染或生产容量。其他本地验证记录并未全部随仓库发布，不应把本页当作这些证据的下载入口。

## 启用边界

`MAIMI_MONITOR_ENABLED` 默认为 `false`。默认不会注册监控路由、启动统计写入或读取采样文件。即使显式启用，监控配置、文件权限或存储错误也应与普通聊天隔离；部署前仍须按 [部署说明](../deploy/monitor-candidate/README.md) 核验。

宿主采集器能接触 Docker socket；该 socket 本身**不是只读权限接口**。安全边界依赖受信任的固定采集程序、受控安装目录和宿主权限；网关仅获得私有采样目录的只读挂载及统计目录的写权限。统计含员工归属元数据，不应公开采样快照、SQLite、真实运行日志或生产配置。

本目录提供源码和部署候选，不执行公司维护，不改变 service 的三人授权，也不自动启用集中沙箱。正式上线仍遵循现有堡垒机流程和独立验收。
