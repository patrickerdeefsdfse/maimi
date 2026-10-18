# r65 网页迁移独立审查

范围：`web/prepare-runtime.py`、`enterprise-profile.mjs`、`community-profile.mjs`、`company-workspace-controller.mjs`、`verify-runtime.mjs`、`pack-runtime.py`、Dockerfile，根目录 `bootstrap-source.py`，以及聚合项目 worker 的正常退出改动。未修改 root 文件，未执行公司部署。

## 已验证

- 使用真实 Cordis、Peer、Typert Gateway、RemoteAccess，以及官方 `workspace-controller/typert` 生成描述符，直接调用 Remote（不经过 worker HTTP 代理）证明子类守卫生效。非 `/workspace` 的 create 未触达 registry；合法路径保留原生 create；rename 限固定名称、delete 拒绝、initializeDefault 不产生写入。非 owner 仍被原生 remoteAccess 拒绝。测试文件 `workspace-controller.test.mjs` 1 项通过，覆盖多个正负场景。
- `prepare-runtime` 不覆盖官方 node_modules bundle；精确核对 DSH 包版本，独立装配 company UI，去掉旧自动重连段。官方品牌替代为公司插件，工作区固定策略作为 controller 子类，符合“上游功能优先、企业边界独立”的迁移方向。
- 社区 profile 迁移只删除列明的四类旧适配器配置，保留其他偏好并留原文件字节备份。异常 YAML 拒绝替换。没有把全部用户插件一并清空。
- `worker.mjs` 新 stopping 状态只将已由 wrapper 接收 SIGTERM/SIGINT 后的正常退出或 SIGTERM 退出标为 0；非预期退出仍非零。它不更改任务授权。Root 已另行完成实际完整 worker 正常 stop/exit0 测试。
- `bootstrap-source.py` 下载固定 commit、校验固定 SHA-256，只解包普通文件/目录，拒绝绝对路径、`..`、反斜杠、重复路径、特殊文件；限制压缩和展开大小。下载失败保留部分归档但不会执行或继续解包；下一次需处理该未完成文件。
- `pack-runtime.py` 拒绝树外符号链接与特殊文件，不调用公司或 Docker，归档条目消除宿主 UID/时间。仅接收 COPIED_COMPLETE_TREE；reference-modules 本地树不能当作完整包。

## 历史复现与修复复核（以下问题均已修复）

1. **原 verifier 曾接受空树清单。已修复并复测。** 合成清单 `packages={}`、`company_source_targets={}` 以前可通过。最新版固定必须包与 company 文件集合，拒绝空清单、遗漏 worker 和缺失文件。
2. **原 official 代码完整性检查只有版本。已修复并复测。** 最新装配器记录 npm 精确锁及完整安装文件/链接清单，拷贝后重新比对；verifier 逐项验证字节、内部链接、缺失和新增模块。它验证的是可信装配输入的一致性，不是独立第三方依赖安全审计。6 项 Linux 负例/正例重跑通过；Windows 的 symlink 测例因主机创建权限限制不能直接执行，Linux 结果为准。
3. **原 packer 会包含未列明的树外层文件。已修复并复测。** 曾在合成树中放入 `etc/unlisted-runtime-file`，verifier 只验证其职责内的运行时目录，打包器却会全部打包。最新 `declared_paths` 只接受清单里的模块、公司文件、candidate-manifest 及合法祖先目录，拒绝额外 `/etc` 和未列明的公司代码；4 项打包范围测试通过。镜像内 verifier 不遍历镜像基础 `/etc` 是合理分工，归档白名单由 packer 落实。

`pack-runtime.py` 的收据同时固定 archive 和 candidate-manifest 摘要，`build-image.py` 在构建前复核归档并将清单摘要传入 Docker ARG；Dockerfile 验证实际解包的安装树。固定本机 base 标识、短暂本地 alias、构建后再次比对 alias、禁用构建网络均已审阅。额外检查 `DOCKER_HOST=tcp://127.0.0.1:65534` 时 context inspect 会返回该覆盖地址，因此本机 Unix socket 检查不会误把此变量覆盖当作默认本机 context。没有公司 Docker 调用。

## 最新原生功能与旧 RPC 策略

直接加载安装包官方 `lib/typert.host.js`，得到 `native-rpc-inventory.json` 的 48 项相关实际 Remote 描述符，不依据 UI 名称猜路由：

- 原新版目标 namespace 为 `goals`，旧正则仅 `goal`；动态执行为 `dynamicCordisRunner/*`，插件安装为 `pluginManager/*`，原先未被精确策略涵盖。独立上游审查代理已交付逐动作 RPC 策略，保留受限写操作，不统一取消拦截。
- 原 `schedule/history` 和新版插件清单/权限预设查询未准确划为只读。上游代理报告完成 20 项新版只读、25 项禁止写、60 项两 lease 查询组合测试，并保留原有 20 项只读；报告和补丁位于 `upstream-audit/rpc-policy/`。此处不重复宣称已做公司业务验收。
- `subagents/prompt`、`interruptByParent` 不被上述禁用正则误挡。上游审查代理随后完成完整本机 worker 的子模型启动、父 lease 继承、合法排队、foreign lease 拒绝、父取消关闭子 SSE、新轮继续测试，记录在 `upstream-audit/native-advanced-tests/advanced-result.json`。冷启动子代理恢复世代、跨登录后台目标仍未适配，不等于这些场景完成。
- 原生审批通过浏览器 Peer 的 `$events`/`$events/result` 关联。后续实际交互测试定位通用 canonical key 校验会挡住 `$events/result`，上游代理已提交仅针对该精确入口的例外，仍按 mutation 校验 lease；没有放开所有 `$` 路由。真实 Gateway+Peer 证明另一 owner Peer 不能代答，同一 Peer 可答，证据在 `upstream-audit/rpc-policy/native-event-peers-result.json`。公司端仍需联合验收。
- 当前 npm 树有浏览器侧栏 UI，但没有安装 `@deepseek-ai/dsh-browser-use` 执行服务及可用浏览器 backend。上游源码里相关自动化仍在 browser-use/experimental 下。浏览器 UI 存在不等于自动浏览器能力可用；不是删除 validateNativeRpc 就能启用，需要确定官方支持路径、浏览器运行环境与权限。
- `remote.mux` 仍只分发流式 Remote；Gateway 的 requireStream/mode 检查拒绝 unary 通过该流入口。不能据此笼统声称普通 prompt 可通过 mux 绕过公司 admission，也不能因为没发现此路径就声称所有流式接口完成安全验收。

以上结论是本地源码/合成验证。没有新的公司服务器状态，不改变 M00003308 的未决诊断，也不等同生产发布完成。
