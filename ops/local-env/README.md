# 本机 Linux 开发环境

本机已安装独立 WSL2 发行版 `MegmeetDev`，Ubuntu 24.04.5，虚拟磁盘在 `D:\MegmeetDev\Ubuntu-24.04`。使用发行版内 Docker Engine，不依赖原有 Docker Desktop。源码仍以 `D:\麦格米特\linux` 为准，Linux 的 `/home/megmeet/project` 指向同一目录，避免两份代码不同步。

## 日常使用

在项目根目录的 PowerShell 调用：

```powershell
& .\new-system\ops\local-env\dev.ps1 status
& .\new-system\ops\local-env\dev.ps1 up
& .\new-system\ops\local-env\dev.ps1 smoke
& .\new-system\ops\local-env\dev.ps1 test
```

- `up`：使用本地锁定镜像构建并启动服务。
- `start`：只启动已经构建的本地镜像，不构建、不拉取；重启电脑或没有下载网络时使用。
- `restart`：修改 Gateway Python 源码后重启 Gateway；源码通过只读挂载直接生效。
- `smoke`：真实数据库、缓存、Gateway 与模拟公司接口的集成检查。
- `test`：在 Linux 中运行本地测试、Gateway、个人入口及续期候选测试。
- `status`：版本和服务状态。
- `stop`：停止本地服务，保留数据库卷。
- `shell`：进入 Linux 终端。
- `lock`：捕获当前正在运行的本地镜像及完整 Python 依赖；只在有意更新依赖并验证后执行。

后端入口是 `http://127.0.0.1:28090/health` 和 `/ready`。已按新授权接入DSH 0.1.6-alpha.1原生界面，桌面客户端加载本机28084；详见 `../../dev-local/dsh/README.md` 和 `../../desktop-local/README.md`。`native-smoke`检查原生入口，`restart-dsh`单独重启DSH。数据库和缓存不向Windows发布端口。

## 版本与环境边界

- Node 22.20.0、pnpm 11.7.0 与服务器此前构建工具版本一致。
- Python 应用使用 3.12 容器；PostgreSQL 16、Redis 7 与已收到的服务器服务主版本一致。
- `new-system/dev-local/locks` 保存基础镜像 digest、完整应用依赖锁、运行镜像 ID 和源码指纹。锁定重建会校验源 requirements、锁文件摘要及平台，避免悄悄更新间接依赖。
- 宿主 Linux Python 测试环境在 `/home/megmeet/.venv`，与应用容器独立。
- Docker Engine 29.8.1、Compose 5.5.1 在本机固定；没有声称它们是服务器当前精确版本。
- WSL 上限为 4GB 内存、4 个逻辑 CPU、2GB swap。这是本机开发配置，不用于推断服务器容量。

公司登录和模型调用使用两个明确的合成账号及模拟接口，真实员工资料、TokenKey、公司密钥和业务数据均未导入。运行服务使用独立内部网络。生产 compose、服务器部署包和现网均未改动。

## 尚缺的服务器快照

尚未获得现网定制 DSH 镜像/运行时 wheel，也未取得当前 PostgreSQL、Redis 以及全部应用镜像的现场 digest。因此这是可运行、可调试并已锁定的本地后端开发环境，尚不是整个服务器的精确副本。用户随后明确要求接入最新版，现已用独立镜像安装官方0.1.6-alpha.1并迁移品牌呈现，原生UI已验证；这仍不代表与服务器0.1.5rc2定制镜像完全相同。真实个人Host/SSO/TokenKey须另验。

## 下载代理与资源

这台电脑已有代理监听 7897。新 WSL 通过其默认网关访问该代理，供 Docker 拉镜像和构建时下载公共依赖。`configure-docker-proxy.sh` 创建 systemd 前置服务，每次 Linux 启动重新解析默认网关。不会向应用容器注入代理；新环境不开放 Docker TCP 管理端口。

关闭电脑代理时，已下载的镜像仍可通过 `start` 启动；`up` 的构建和新增拉取/下载可能失败。电脑代理端口改变后需相应更新本地代理配置。

`up`/`start` 会创建隐藏的 WSL 空闲保持进程，避免命令窗口关闭后 Linux 服务自动停止；`stop` 停止本地服务并结束这个自建保持进程。实现依据是 Microsoft 关于 [WSL systemd 生命周期的说明](https://learn.microsoft.com/en-us/windows/wsl/systemd)。不会创建开机自动启动任务。

虚拟磁盘和容器数据在 D 盘；Windows `.wslconfig` 的资源上限作用于 WSL2，共享资源设置也影响其他 WSL2 发行版。修改已说明，不删除其他发行版或其数据。

安装器 `install-ubuntu.py` 会拒绝重复安装已存在的 `MegmeetDev`。`bootstrap-linux.sh` 与 `configure-docker-proxy.sh` 只允许此专用发行版运行，不应复制到公司服务器执行。
