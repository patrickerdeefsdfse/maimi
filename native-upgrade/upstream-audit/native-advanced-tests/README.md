# DSH 0.2.1-rc.2 原生审批与子任务实测

状态：本机完整 worker 合成验证通过，公司零请求、零修改。基础树 `/home/megmeet/maimi-r65/tree-v4`，仅将独立候选 task-authority.mjs 作为只读文件覆盖挂载；没有改官方 npm 模块。Docker network=none、根文件系统只读、1.5 GiB/512 PID/init，模型服务仅在测试容器 loopback 8080 返回合成 SSE。结果不是生产验收。

## 已验证

- 真实 session/create、session/prompt → 原生 subagent 工具 → 子代理模型请求使用父任务的公司模拟租约。
- 原生 subagents/prompt 对已经运行的真实子代理接受排队消息；错误租约拒绝。
- 取消父会话关闭子代理模型流，父会话恢复空闲；新一轮继续成功。
- 原生 WebSocket remote.mux 的 $events 流收到真实 write 扩权审批。
- 错误租约无法代答 allowed-once；正确租约的 rejected 回传成功，日志产生对应 approval/asked 与 approval/decided，目标文件没有创建。
- 第二次待审批时取消会话，客户端收到 cancel 通知，日志 outcome=cancelled，文件仍未创建。
- 九次模拟模型请求、十七次模拟租约检查；所有测试数据均为虚构内容。

原生 same-Peer 身份约束另由 rpc-policy/check-native-event-peers.mjs 验证：真实 Gateway + Peer + Remote event 源，只替换连接载体；其他 owner Peer 或不支持 interactions 的 Peer 不能答复当前事件，同一个 Peer 成功，已完成事件重放无作用。结果保留在 native-event-peers-result.json。

## 复现

```powershell
wsl -d MegmeetDev -- python3 '/mnt/d/麦格米特/linux/output/dsh-upgrade-r65-20261018/upstream-audit/native-advanced-tests/run.py' --tree /home/megmeet/maimi-r65/tree-v4 --policy '/mnt/d/麦格米特/linux/output/dsh-upgrade-r65-20261018/upstream-audit/rpc-policy/task-authority.mjs'
node --expose-internals output/dsh-upgrade-r65-20261018/upstream-audit/rpc-policy/check-policy.mjs
node --expose-internals output/dsh-upgrade-r65-20261018/upstream-audit/rpc-policy/check-native-event-peers.mjs
```

两个 Node 检查都支持 DSH_R65_RUNTIME 指向含 package.json/node_modules 的完整官方运行时；不设置时回落 ../upstream/runtime-win。

## 限制

这里只验证活跃父任务内的子代理授权和取消、审批拒绝/取消。未宣称冷恢复的子代理可重新申请公司任务世代，未启用或验收跨登录后台 goals/schedules、动态插件安装；这些仍需企业权限适配。仅凭原生能力存在不能说所有新版功能已启用。没有复用任何公司 Key/聊天/附件或连接公司 SSH。

## 合并

上一轮 task-authority.mjs SHA 445881281fbd8b3dc3597d90b9358f6f6369bbf2ef63d3dfcdbdcb80089ffb08。
本轮 SHA b09756fc9ed4b489571ee110951b684b01c95be3070a9c20e8c02f93f715cfab。
修正仅允许官方精确保留接口 $events/result 通过规范路径校验，仍是 mutation，保留企业租约检查与原生同 Peer/事件绑定。其它 $ 命名空间未放行。
