# 不可变预装 runtime 镜像：构建与验证设计

状态：设计已核对固定上游源码；镜像尚未构建，启动路径尚未替换，真实 runsc 未验收。此设计不改变当前关闭状态，不含公司执行命令。

## 选择

将 execd 和启动文件预装进经过批准的任务镜像，创建时只允许登记过的最终镜像 digest。运行时二进制保持只读；临时数据、日志、HOME、工作文件使用独立有界 tmpfs。避免每次任务在未启动容器中写入程序，也避免为注入程序临时开放可写根目录。

不能只改 Dockerfile：需要同时修改并验证启动钩子、挂载目录、运行时可写路径及节点核验。原有 `/opt/opensandbox` tmpfs 在新方案里移除，不能遮住预装程序。现有 provider/node_guard/server_policy 仍为旧候选配置，未擅自改成假定可用的预装方案。

## 构建输入及产物

1. 在本地隔离构建环境确认 linux/amd64 基础 Office 镜像 digest、官方 execd 镜像 digest，以及固定 1.1.1 源码。不得使用浮动 latest/tag。官方标签为未签名标签，HTTPS 获取与内容哈希不等于供应链签名证明。
2. 只在构建阶段从固定 execd 镜像提取明确清单：`/execd`、`/bootstrap.sh`，以及审查后选用的 supervisor/session gate/launcher。二进制、脚本逐项哈希和架构核对。无需运行任何用户任务或给构建环境公司凭据。
3. 将必须文件放入最终镜像的 `/opt/opensandbox/`，root 持有、不可修改的文件模式。基础镜像提供 POSIX shell、必要动态库及固定 Python/Office 依赖；保留源许可证和依赖清单。不启用 eBPF 变体，不增加 CAP_BPF、CAP_PERFMON 或 SYS_ADMIN。
4. 在构建产物中保存构建清单：基础镜像 digest、execd 镜像 digest、源提交、文件哈希、软件版本、最终镜像 digest。运行时只接受管理员登记的最终 digest；镜像自报标签只作辅助，不作为独立信任依据。

## 启动适配

- 将 `_prepare_sandbox_runtime` 的“拷贝程序”替换为“验证批准的预装镜像与文件存在”路径。只有固定来源的策略钩子可替换这一方法，并新增 `runtime.py` 来源哈希检查。版本、文件、镜像或钩子不匹配即拒绝创建。
- 不再进入上游 `_fetch_execd_archive` 的临时容器创建流程；它与当前强制 runsc/只读策略的沙箱创建钩子是不同路径，不能假定自动受到同样约束。
- `/opt/opensandbox` 始终在只读根中。新有界 `/run/opensandbox` 用于 `.env` 等运行状态，`/tmp` 和 `/workspace` 保持现有限额；HOME/缓存/日志的确切位置需沿固定 bootstrap 和 execd 源码逐项核对，不能用宽泛可写 `/root` 或宿主挂载兜底。
- `EXECD_ENVS` 改至 `/run/opensandbox/.env`；它只保存任务允许的环境配置，不含 API 密钥或公司凭据。`EXECD_INIT=1` 保持，启动后核对实际 PID 1 与孤儿进程回收。
- 预脚本、外部凭据代理、用户自定义环境、卷、扩展及任意入口不开放。普通命令仍在固定 `/workspace` 执行，模型无法选择镜像、运行时或 Docker 配置。
- 改动必须一起更新 server_policy、node_guard 的允许 tmpfs 集合和策略版本，避免服务端允许而客户端误判，或客户端误信旧策略。

## 构建与启动验收顺序

1. **静态构建校验**：审核所有输入 digest、文件清单、架构和许可证；确保镜像没有秘密、包管理缓存、业务文件或 socket。检查所有必需程序及 Office 依赖，不以目录存在代替实际 import。
2. **本地真实启动**：使用真实 runsc、只读根、cap-drop ALL、no-new-privileges、固定 PID/内存/CPU、有界 tmpfs、隔离网络启动；首次与重复启动均通过。创建过程不得执行 put_archive 或临时开启可写根。保留固定状态与版本证据，不记录用户命令内容。
3. **边界负向测试**：写只读路径失败；不能访问宿主、相邻沙箱、公司网、Docker socket；公开端口不能访问；写满 tmpfs、fork/线程峰值、OOM 和超时后仍可由控制面回收。
4. **任务兼容测试**：固定合成输入完成 Python、PPT→PDF→逐页 PNG、文件导出；原始公司模板另经已授权本地验证。确认工作数据只留在任务 tmpfs，程序不可被任务改写。
5. **恢复与清理测试**：创建丢回包不二次创建，命令丢回包不重放，pause/unpause 任一未知不交付文件，删除未知不释放预算。控制面重启后从持久台账恢复核对。至少连续多轮任务观察僵尸、线程、内存和容器残留。
6. **准入回执**：把以上验收绑定最终镜像 digest、Docker/runsc/内核版本、策略哈希、网络和规则摘要。配置变化使验收失效；缺少回执时 provider 继续关闭。通过后再进入平台层联调和单独公司部署审查。

## 已核对的固定上游路径

- `server/opensandbox_server/services/docker/container_ops.py`：先 create，再 `_prepare_sandbox_runtime`，最后 start。
- `server/opensandbox_server/services/docker/runtime.py`：ensure directory、execd、bootstrap 均使用 put_archive；含独立 execd 临时容器路径。
- `components/execd/bootstrap.sh`：默认 `EXECD_ENVS=/opt/opensandbox/.env`；`EXECD_INIT=1` 分支 exec 到 execd。
- `components/execd/Dockerfile`：官方镜像内原始文件位置及二进制构建来源；它的浮动基础 tag 不可直接作为本项目固定构建输入。

以上是源码证据和实施计划，不能代替真实 Docker/runsc 运行证据。
