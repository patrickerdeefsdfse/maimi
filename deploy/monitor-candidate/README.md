# 单机监控部署候选

这些文件仅用于后续部署评审，没有安装服务、启用监控或修改现有 Compose。公司仍按原版本运行。`MAIMI_MONITOR_ENABLED` 固定为 `false`；systemd 单元没有 `[Install]`、定时器或自动重启，启用门文件也未创建。本目录不提供现场执行命令。候选的静态检查通过不等于这些 systemd 限制已在公司宿主实际运行验证。

源码整合位置为 `deploy/monitor-candidate/`；采样代码的源码位置为 `tools/monitor-collector/`。未来维护包需把经哈希核验的 `collector.py`、`storage.py`、`cache.py`、`daemon.py`、`sample.schema.json` 和对应说明安装到 root 所有、不可由员工或网关写入的 `/opt/maimi-monitor/collector/`。`python3 -E -s -B` 忽略用户 Python 环境与用户 site，保留脚本目录供相邻模块导入；不能直接替换为 `-I`。另须预建 `/opt/maimi-monitor/docker-empty-config`，root:root、0555、完全为空且路径无符号链接；采样器每次验证后显式传给 Docker `--config`，条件不符即拒绝，不读取宿主原有 Docker 登录配置。

## 两条独立路径

宿主采样器只使用本机固定 `unix:///var/run/docker.sock`，访问受限 Docker 元数据投影、宿主 `/proc`、cgroup v2 和经过卷归属校验的目录元数据。它不开放 HTTP、TCP 或任意命令接口，不接受网页提交的路径，不执行容器内命令。systemd 限制文件系统写入、IP 网络、能力集合、子进程数和资源使用。Docker CLI 仍是直接访问本机守护进程：**Unix socket 的只读挂载不能把 Docker API 变成只读**，命令白名单依赖经审查且不可篡改的采样程序。此 root 服务属于可信平台代码；这些限制不能被描述为容器逃逸隔离或通用 root 命令代理。没有 sudoers 规则，也不向网关、用户、模型或 SSH/service 插件授予 Docker socket。

网关仅消费严格校验过的缓存，保存自己的聚合记录并通过既有 IAM 提供只读页面。Compose 范本只增加以下两个绑定，既有网关依赖保持由正式部署负责：

| 宿主位置 | 容器位置 | 权限和用途 |
|---|---|---|
| `/run/maimi-monitor` | `/run/maimi-monitor` | 宿主目录 root:10001、0750，`latest.json` root:10001、0640、单链接普通文件；网关只读 |
| 未来维护包绑定的独立持久目录 | `/var/lib/maimi-monitor` | 目录为已核实网关有效 UID 所有、0700；SQLite 文件私有，网关可写 |

必须挂载缓存**目录**，而不是单个 `latest.json`，否则采样器原子替换后容器可能继续看到旧 inode。源目录必须预先存在，范本禁止 Compose 自动创建。缓存与 SQLite 目录不能相同，不能位于业务工作区，也不能映射 Docker root、宿主根目录或凭据目录。`/run` 会在重启后清空，未来维护包需要显式安排受控的私有目录重建及启动顺序；当前范本没有加入 tmpfiles 或开机启用机制，目录缺失时保持停用。

采样缓存的 GID 10001 是当前候选代码契约，不是对未来镜像的猜测。若实际网关主组或补充组没有这个 GID，必须先统一并验证发布契约，不能 chmod 为公开可读、以 root 跑网关或随意扩大组权限。systemd 的 `CAP_CHOWN` 用于缓存归属，`CAP_DAC_READ_SEARCH` 用于只读目录元数据，`CAP_SYS_PTRACE` 用于不同 UID 的 PID/网络命名空间元数据读取。这些能力也具有更广的内核权限，不能据此声称操作系统只能读取元数据；源码白名单和不可写代码目录仍是必要边界。若公司 `/proc` 隔离策略或安全模块拒绝访问，应显示未知并复核，不通过添加 `CAP_SYS_ADMIN`、特权容器或 TCP Docker 端口解决。

## 可见范围

只有 M00096389、M00000246、M00091350、M00063756、M00003260、M00057343 获得 `monitor:read`。后台每次请求核验 IAM，环境变量只能缩小源码中的六人名单。该名单不授予 service、SSH、执行命令、重启或其他管理写权限。工作区标签中的 UUID 仅是候选归属，必须由后台数据库再次核实后才关联员工。浏览器看不到缓存文件、Docker socket 或监控数据库。

停用时网关不注册监控路由、不读监控目录、不消费计量；业务聊天继续原流程。单独启动宿主采样器仍会产生私有元数据，因此未来正式启用需要同时核实网关开关与宿主服务状态，不能只改网页按钮。缓存过期、采样失败、缺少桌面或业务计量时显示未知，不显示为零用量。

## 正式维护包仍需绑定的门禁

1. 精确核验当前网关/管理器版本、目标网关不可变镜像、监控源码和 UI 哈希、真实启动接线及六人策略。范本的 `MAIMI_MONITOR_BOUND_*` 必填值没有默认值，不接受仅 tag 未校验的镜像。
2. 核对宿主 Python 3.10+、systemd 版本及实际生效的硬化属性，固定 Docker CLI/socket、cgroup v2、Docker root 和 `/data`。在同类环境验证完整单元下两次采样、正确速率、超时停止、目录扫描预算、重启失效和资源开销。当前只实测过采样代码，没有启动本单元。
3. 按真实 UID/GID 预建两个互不重叠的私有目录，检查全部父目录归属、无符号链接、ACL/挂载传播、剩余空间及保护策略；禁止宽泛 chown/chmod 或清理既有目录。门文件仅是缺省关闭条件，**不能替代**这些校验或批准回执。
4. 备份现有 Compose/镜像引用及监控状态（若已存在），SQLite 备份采用停写或一致性接口，不直接复制活跃数据库的单个主文件。记录可恢复的旧引用和目录，确认不覆盖员工文件、不改数据库业务结构、Office 或服务凭据。
5. 私下解析合并后的完整 Compose 并核对差异。override 会与既有挂载合并，单看这份范本不能证明最终容器没有多余权限；完整结果可能包含私有配置，不粘贴进聊天。只允许新增上述两处挂载及经评审的监控环境变量/镜像，禁止向网关添加 Docker socket、host PID/network 或宿主业务目录。
6. 有限维护验证：默认关闭时聊天可用，开启后六人能读、其他人被拒、工号头不能伪造、owner 必须经数据库匹配；监控目录错误或 UI 缺失不阻断普通聊天。完成后才能写入正式部署/验收回执。本目录没有自动切换、重启、安装或回滚执行器。

systemd 配置语义参考 [systemd.exec 官方手册](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)；Compose 绑定和合并需按 [Compose services 官方说明](https://docs.docker.com/reference/compose-file/services/) 在目标版本核验。当前范本并不把新版手册中支持的选项视为公司宿主已支持。
