# 运维脚本

这些脚本只用于新系统的独立测试目录，不会删除旧项目、旧卷或旧镜像。

1. 复制 `.env.example` 为 `.env`，填入测试数据库密码和测试服配置；不要把 `.env` 提交 Git。
2. 执行 `bash ops/bootstrap-test.sh`，它只启动 `core` profile 的 PostgreSQL、Redis 和 Gateway。
3. 执行 `bash ops/check-test.sh` 查看容器状态、Gateway 健康和 Compose 配置。
4. 通过 DSH、SSO、模型、插件、文件和恢复验收后，才进入切换手册；旧系统清理不在这些脚本中。

后续的 backup、restore 和 rollback 脚本也必须默认指向新系统独立目录，禁止误操作旧项目。

原生 DSH Web 接入当前使用 [`DSH-NATIVE-WEB-PROBE.md`](DSH-NATIVE-WEB-PROBE.md) 中的一次性断网检查。它复用运行中 Gateway 的镜像，但在独立临时容器执行，不重启 Gateway、不挂载业务卷和密钥，不调用模型。
