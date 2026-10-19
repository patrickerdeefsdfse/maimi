# 桌面 service 服务端授权回归

测试直接导入同仓库 `../gateway/app/local_service.py` 与 `../service-broker/`。不需要临时 monday 目录、不读取公司配置/凭据、不访问公司 SSH；所有工号、密码与 IAM token 都是固定合成测试值。

在仓库根目录创建隔离 Python 3.11+ 环境并安装测试依赖，然后运行：

```text
python -m pip install -r services/local-enterprise-tests/requirements.txt
python -m unittest discover -s services/local-enterprise-tests -p test_local_service.py
python -m unittest discover -s services/local-enterprise-tests -p test_retirement.py
```

网关 10 项涵盖真实桌面 grant 读取、三工号名单、员工/设备/原生会话绑定、控制许可与工具许可隔离、撤销和 broker 契约。broker 5 项使用真实 SQLite Journal/Connections 与合成 SSH transport，覆盖退役 ACK 后迟到 POST、重启持久性、吞掉取消的迟到 SSH 返回、普通断开仍可重连、新代不被旧代退役误伤。

## 可选的真实 Redis 7 测试

需要已有本机 Docker Engine 和本地 `redis:7` 镜像。测试会先 image inspect，run 带 `--pull never`，不会自动访问注册表。容器随机命名、network none、无公开端口、只读根目录、临时 /data，结束清理。请明确使用本机 Docker context，不要指向生产引擎。

Linux 或已配置 Docker Desktop 的 Windows：

```bash
MAIMI_LOCAL_REDIS_TEST=1 python -m unittest discover -s services/local-enterprise-tests -p test_real_redis.py
```

```powershell
$env:MAIMI_LOCAL_REDIS_TEST = '1'
python -m unittest discover -s services/local-enterprise-tests -p test_real_redis.py
Remove-Item Env:MAIMI_LOCAL_REDIS_TEST
```

Windows 如需特定的**本机** WSL 引擎，可显式设置 JSON argv；不是 shell 字符串，不默认任何发行版：

```powershell
$env:MAIMI_TEST_DOCKER_COMMAND = '["wsl","-d","YourLocalDistribution","--","docker"]'
```

未设置时用 `docker`。未显式启用 `MAIMI_LOCAL_REDIS_TEST=1` 时测试跳过。真实 Redis 回归验证原子 grant 创建/换代、旧代撤销不删除新代、许可类型、设备隔离、锁 CAS 和 Redis 中无明文 capability。此测试不需要公司 Redis 或任何端口。

当前证据保留在 `original-validation.json`；仓库迁移后的复测另见 `repository-validation.json`。原 39 项 broker 回归是先前证据，本轮只迁移并复测上述与路径修改相关的测试，不重复声称执行了全套。
