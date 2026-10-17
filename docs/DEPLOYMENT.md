# 部署说明（后续用于公司 Linux）

1. 将仓库放入独立目录，例如 `/data/projects/megmeet-ai-new`。
2. 准备服务器上的 secret 文件和 `.env`，不要从 Git 拉取真实密钥。
3. 先执行 `docker compose --profile core config` 检查配置，再启动 core。core 会先运行一次 `db-migrate`，按文件名顺序以校验和记录迁移；随后启动 gateway 和 `retention-worker`。这一步覆盖已有 PostgreSQL 卷，避免只依赖首次初始化脚本。
4. 检查 gateway 的 `/health`（存活）和 `/ready`（迁移、PostgreSQL、Redis 均可用）后，再按需启动 vector/search。
5. 若启用 DSH，先把通过验收的 SDK/runtime wheel 作为受控构建输入安装到镜像，并保持 `DSH_ENABLED=false` 完成基础验收；确认模型 transport、profile、workspace 和密钥文件后再打开。
6. 先使用独立测试入口验证登录、会话、模型、插件、文件和回滚，再接入正式 Nginx。

当前文档是部署模板，不代表已经在公司 Linux 执行。

## 历史清理 worker

`retention-worker` 复用 Gateway 镜像中的数据库访问代码，只连接 PostgreSQL；它不会输出会话内容或标识符。通过 `RETENTION_INTERVAL_SECONDS`（默认 3600 秒）和 `RETENTION_BATCH_SIZE`（默认 500，最大 5000）控制频率和单批规模。数据库暂时不可用时只记录异常类型并等待下一轮，不会让 Compose 栈退出。
