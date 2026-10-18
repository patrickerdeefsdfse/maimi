# 麦米网关镜像

网关负责企业身份、租约、业务连接器、项目任务与文件授权。最新版 DSH 的 Node 运行时在独立 worker 中装配，不通过本目录的历史 Python wheel 安装。

## 从源码构建

在仓库根目录执行：

```sh
docker build --platform linux/amd64 -t maimi-gateway:local services/gateway
python3 services/gateway/tests/gateway-image-smoke.py --image maimi-gateway:local
```

构建使用固定摘要的 Python 3.12 基础镜像，并安装 `requirements-linux.lock.txt` 中的完整版本集合。`requirements.txt` 记录业务直接依赖；升级时两份文件应同步审查。这里锁定的是包版本，尚未锁定各平台 wheel 的文件哈希。默认构建需要能访问 Python 包索引，不需要任何公司账号或令牌。

镜像明确包含 `app` 和 `maimi_projects`。后者以前只存在于源码和部署挂载中，遗漏于 Dockerfile；仅复制 `app` 无法重现完整企业能力。修复同时补齐这些源码实际导入的依赖：

| 直接依赖 | 用途 | 固定版本 |
| --- | --- | --- |
| Pillow | 照片、工卡图像、图纸图像处理 | 12.3.0 |
| openpyxl | 工卡与会议清单读取 | 3.1.5 |
| qrcode | 工卡二维码 | 8.2 |
| python-docx | 工卡打印文件与会议文件 | 1.2.0 |
| fonttools | 工卡字体覆盖字符检查 | 4.65.0 |
| pypdfium2 | 热成本图纸 PDF 页渲染 | 5.13.0 |

这些版本来自已经存在的本地 `maimi-r45-local:gateway-import` 镜像包元数据，相关 Office 包版本也与仓库 Office Linux 锁文件一致。没有把整个 Office 服务依赖集塞入网关。

## 配置与健康检查

公司字体、工卡素材、会议母版、模型凭据、数据库、Redis 与企业服务地址由部署配置提供，不会因构建镜像自动生成。未配置业务素材时保持原有 unavailable/limited 行为，不能将空素材目录视作业务验收成功。

`/health` 检查进程可响应；`/ready` 验证数据库所需表、指定迁移和 Redis。没有配置依赖时 `/health` 可返回 200，`/ready` 应返回 503。就绪检查为数据库查询提供唯一列名，并检查 `dict_row` 的值，防止缺表时错误报告 ready。

本地 smoke 脚本启动带随机前缀的临时容器和仅内部可达的 Docker 网络；不发布宿主机端口，不挂载公司数据，结束时清理自己的容器和网络。它需要本地已有 `postgres:16`、`redis:7` 镜像，覆盖：

- 全部业务模块可导入。
- 合成 Excel 工卡名单解析、DOCX 往返、二维码 PNG、PDF 栅格化。
- 实际 PostgreSQL `dict_row` 下，空库、缺表、缺迁移返回 503，齐全返回 200，再删表返回 503。

这些检查不代替公司 IAM、加密上传、安全网关、真实企业数据或员工浏览器验收，也不会连接公司服务。

## 历史 Python DSH 适配器

`vendor-wheels/.gitkeep` 保留可选 COPY 输入，默认为空。只有验证历史 Python SDK 适配器时才放入已批准的 `deepseek_harness_runtime_bin` 和 `deepseek_harness_sdk` wheels；这些部署输入不得提交仓库。未单独验证该历史路径时保持 `DSH_ENABLED=false`。最新版 DSH 的 npm 安装和原生 worker 由其他构建目标负责。
