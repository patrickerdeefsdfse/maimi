# DSH 0.2.1-rc.2 全面升级候选

此目录是 **r65 开发候选**。公司当前仍是 r62 / DSH 0.2.0-rc.2，正式桌面仍是 0.13.0；这里的源码和测试不等于已更新员工客户端。

目标是让网页和桌面复用完整新版原生对话、工具、文件、插件、Office 和桌面能力。企业 IAM、模型许可、安全网关上传、业务连接器、公司 PPT 模板、service 和员工隔离作为独立适配保留。旧的官方 bundle 覆盖不进入新版运行时；重叠的社区统计、主题及文件变更适配退出默认配置。

## 可复现输入

- DSH `0.2.1-rc.2`，官方 **prerelease**，commit `687f282e8625c6b8d11ff58211a90524be2e4317`。
- npm `latest` 检查时仍指旧版，必须使用本目录的精确锁。
- `upstream/source-pins.json` 固定官方源码归档和 Linux/Windows 依赖锁。
- Linux 完整安装保留 LibreOffice WASM；不得按旧构建方式删除该模块。
- 桌面官方源码用 `bootstrap-source.py` 获取并校验，再按 `desktop/README.md` 创建独立构建目录。不要把官方安装器换名字后当成麦米版发布。

## Linux 工作区构建

以下在隔离开发机执行，从仓库根目录开始。依赖安装目录建议放在 Linux 本地文件系统；Windows 挂载盘的大量小文件会显著拖慢启动测试。

```bash
npm ci --prefix native-upgrade/upstream/runtime-linux --no-audit --no-fund
python3 native-upgrade/web/prepare-runtime.py \
  --runtime native-upgrade/upstream/runtime-linux \
  --project . --destination /absolute/new/runtime-tree
node native-upgrade/web/verify-runtime.mjs /absolute/new/runtime-tree
python3 native-upgrade/web/pack-runtime.py \
  --tree /absolute/new/runtime-tree \
  --output /absolute/new/build-context/runtime-native021.tar.gz
```

`prepare-runtime.py` 要求全新输出目录。`--reference-modules` 仅用于本机调试，其输出被打包校验拒绝。将 `web/Dockerfile` 与 `web/verify-runtime.mjs` 放进归档所在构建目录，使用已经核验的 Linux/amd64 基础镜像摘要构建；仓库不猜测生产镜像。

`web/build-image.py --base-image-id <已核验的本机镜像ID> --context <构建目录> --tag maimi-native021-worker:<候选标签>` 只接受本机 Docker 和固定配置摘要，复核归档及清单摘要后构建。它用短暂的本地标签传递已核验镜像，避免把 Docker 配置摘要误当仓库名称下载。

网关可以单独从 `services/gateway/Dockerfile` 构建。它包含完整业务模块及固定 Python 依赖，与 DSH 工作区镜像是两个不同制品。

## 已取得的本地证据

| 验证 | 结果与边界 |
| --- | --- |
| 完整新版工作区 | 本机 Docker、隔离网络、合成身份启动成功；原生 HTML、鉴权、私有路由和路径限制通过，正常停止；不是公司基础镜像验收 |
| service 输入区域 | 真实新版 ChatView、Slots、React 与 Host 插件发现通过；原生及 PTC 路径均显示 IP/密码、可输入和合成提交；未连接公司 SSH |
| 企业技能 | 最新 Skill 服务注册并读取 11 项指引通过；物流及 review 插件更新自身 peer 契约，不授予版本豁免 |
| 原生子代理及审批 | 完整 worker 的委派/追加消息/父取消/继续、审批拒绝/取消通过；不同登录租约和 Peer 不能代答，无公司模型请求 |
| 原生 Office | 官方合成 PPTX → PDF → PNG 成功，转换器释放；缺失 Arial 仍会替换字体 |
| 旧历史样本 | 51 份官方旧样本，新旧 reader 同为 46 成功、5 缺 fixture 上下文；未证明任意员工历史可无损升级或降级 |
| 网关 | 真实 PostgreSQL/Redis 就绪检查及合成 XLSX/DOCX/PNG/PDF 处理通过；没有公司业务请求 |
| 管理器与历史备份 | 8 项管理器、10 项快照单元及 1 项本机 Docker 合成卷集成通过；生成备份及二次校验回执，未实现完整上线/恢复编排 |
| 桌面 | 32 项 IAM/凭据/模型流/退出等测试及官方完整构建 7/7 通过；Windows 安装器、真实登录、本地企业文件桥与完整 Office payload 未验收 |

完整合成对话还覆盖中文附件、模型流、取消后继续发送以及其他登录凭据不能取消当前任务。打包器对全部安装文件和内部链接逐项校验，拒绝空清单、缺失文件、额外文件及同版本被修改的代码；这校验的是已锁定安装的装配一致性，不是第三方依赖安全审计。

详细记录在 `web/worker-smoke.json`、`gateway/validation.json`、`desktop/validation.json` 和 `upstream-audit/`。测试报告只说明其列出的范围。

## 不能跳过的发布步骤

1. 在公司进行完整对话、取消、附件和业务插件的联合验收，确认公司加密文件仍走认可的上传协议；本地合成验证不能代替安全网关实测。
2. 管理器仅接受核验后的新镜像；不能把旧版 `node_modules` 覆盖挂载到新版。
3. 在停写后的历史副本演练，保存升级前快照；新版产生的数据不得直接交给旧版写入器。
4. 桌面完整构建、受控更新与本机企业文件授权验收。新候选使用独立应用身份，不覆盖旧安装。
5. 通过已有堡垒机流程安排维护及员工验收。健康 READY 不替代功能验收。

新版后台目标、自动定时执行、动态宿主插件安装及自动浏览器后端尚需企业授权和生命周期接入；对应入口不会因为升级版本号就默认开放。原生只读历史、插件清单和权限预设查询已按最新 API 调整，保留可用的查看功能。

新任务沙箱按 [新版执行接口](upstream-audit/SANDBOX-API-MAPPING.md) 接入原生 fs/subprocess/shell/PTC。r64 继续默认关闭；不把同机容器或 WSL 的 partial Landlock 说成完整隔离。此前新用户的实际入口故障仍待周一现场证据。
