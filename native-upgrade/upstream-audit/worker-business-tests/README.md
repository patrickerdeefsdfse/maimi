# 完整候选工作区的本机业务验证

2026-10-18，`/home/megmeet/maimi-r65/tree-v2` 中真实 DSH 0.2.1-rc.2、企业 profile 和适配器已完成本目录测试。结果 `business-result.json` 为 PASSED。测试运行于本机 Docker，网络模式 none，仅把 `r65-no-network` 解析为容器 loopback；测试进程在容器内 8080 提供假模型网关。没有访问公司服务器、员工目录、真实密钥或模型供应商。

## 已验证

- 完整 worker 启动（该次约 9.34 秒），从 3084 认证入口创建原生工作区会话。
- 无效 client lease 被拒绝。合法创建和 prompt 均经过原 worker 的 task authority、代际、native receipt、作用域模型 ticket 和 lease 校验；没有直接调用裸 3085 绕过入口。
- 模型 SSE 经本机假网关、企业 wrapper 和最新原生 Agent 提交，答案能通过真实 session/projections + session/page 读取。
- 带中文的合成 txt 经原生二进制上传入口，URL 保留 filename/size 参数；文件名、长度、SHA256 和保存的实际字节逐项匹配。
- 原生上传 receipt 随 prompt 被接受，持久用户消息保留附件引用，模型收到对应文件路径。
- 流式输出中，其他 client lease 的取消被拒绝；合法取消获得原生确认并关闭假网关上游流。
- 取消后同会话的新 prompt 成功，确认旧代际不会阻塞后续消息。

共 5 次本机合成模型请求（含原生标题生成），13 次本机 lease 校验。测试结束停止并删除自己的容器，tmpfs 中的合成状态随之删除。

## 可复现

在本机 WSL `MegmeetDev` 中执行：

```sh
python3 /mnt/d/麦格米特/linux/output/dsh-upgrade-r65-20261018/upstream-audit/worker-business-tests/run.py --tree /home/megmeet/maimi-r65/tree-v2
```

`run.py` 只接受本机 Unix Docker socket，使用预先存在的本机测试镜像，不拉取镜像、不改变安装目录。该镜像是本机替代镜像，**不是公司当前生产镜像**。测试失败返回非零退出状态。

## 接口细节与边界

新 Typert 的 `session/list` 参数名是 `payload.args._request`，而 `session/create`、`prompt`、`page` 等为 `payload.args.request`。RPC 错误可能是 HTTP 200 下的 `result.ok=false`，不能只检查 HTTP 成功。

这不是浏览器完整端到端测试，也不验证公司加密安全网关实际解密、SSH 连接、生产模型可用性或公司历史迁移。本机通用上传通过不能替代加密文件上线验收。`business-checkpoints.json` 仅包含本测试制造的会话和文件引用，不含员工数据。
