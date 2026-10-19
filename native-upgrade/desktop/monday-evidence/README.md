# r65 周一桌面持续登录：历史验证证据

本目录保留设备续登单项完成时的证据，当前整合状态以 [桌面 README](../README.md) 与 [企业桥接线](../tests/enterprise/INTEGRATION.md) 为准。本文不是当前整合包的完整功能清单。

本目录是 `desktop/` 的独立增量候选，目标仍为 DSH `0.2.1-rc.2`。未修改 `desktop/`、`project/`、`publication-public/`、已安装麦米或公司服务；没有复制 node_modules 或完整上游源码。

## 已实现

- Electron 主进程把真实 `safeStorage` 对象传给 `company-desktop-session.mjs`，设备凭据只经 OS 加密后落在候选 userData 下。
- 启动先恢复已记住的公司设备登录；首次、过期或明确拒绝才进入原公司 IAM PKCE 登录。
- 复用已发布代码的设备轮换 journal：服务器可能已提交而响应丢失时，保留同一 request ID 和下一枚 proof，禁止因为网络失败直接覆盖旧设备。
- 安全存储不可用时仅做本次网页登录，不降级明文、不覆盖原加密文件。Linux basic_text/unknown 后端不保存凭据。
- 普通关闭/重开保留设备。原生菜单增加“退出公司登录并关闭麦米”，沿上游 Windows 菜单 IPC 的发送方校验进入原生确认和任务锁；确认正在运行的任务后，先保存本机禁止续登标记，再关闭原生视图和 Host，最后撤销设备并关闭应用。离线撤销保留 journal，退出失败也不自动重新登录。
- 退出逻辑复用上游 `DesktopQuitConfirmation` 和后端关闭流程，不新增可供网页任意调用的退出 IPC。并发点击合并；取消确认恢复任务准入；保存禁止续登标记失败时不停止 Host、不撤销设备。
- 修正开发态 Office payload 路径：删除自造 `.desktop-build/primary-runtime` fallback，保留上游 `developmentPrimaryRuntime()` 与开发启动器设置的 `DSH_DESKTOP_PRIMARY_RUNTIME_DIR`；官方 resolver 在 Windows ARM64 上也指向 `targets/win-x64/runtime/primary-runtime`。
- 本机模型桥的临时凭据变量统一使用 `MAIMI_LOCAL_MODEL_TOKEN`，符合原生 subprocess 的凭据清除规则；不修改上游清除函数。真实原生 `scrubbedParentEnv` / `childEnv` 和实际子进程验证了该合成凭据不会继承到任务命令。

`company-auth/remembered-store.cjs` 和 `device-session.cjs` 从 GitHub 首次已发布源码原样复用，SHA256 分别为 `82b0adad3ee1a7d6b2c3956723d3ecbee2dafe550e623c779abfcd64106d1ab2`、`ef4147739f4fa68507e6c22d01965919f03d8ba122a7d50de175c1074e295661`。

## 验证

以下是原独立增量的命令记录；整合仓库请在上级 `desktop/` 目录运行对应测试，不要在本证据目录执行：

```powershell
node --test --test-concurrency=1 tests/*.test.mjs
python scripts/refresh-patch.py
```

- 26 项设备持久化、断网轮换、撤销、取消、禁止明文、开发路径、真实 shell 适配器、退出及原生子进程凭据边界测试通过。其中一项执行从固定上游 `main.ts` 实际变换得到的菜单闭包，验证“锁→原生任务确认→禁止续登→关视图→关 Host→撤销→退出”的次序；另有独立临时目录验明准备脚本实际复制和变换结果。
- 1 项真实 DSH Host 双启动通过：合成 IAM 首次登记→原生 Host→正常关闭→同一账户记住登录恢复→第二次原生 Host→显式退出后旧 URL 失效。最终源码套件共 27 项通过、0 失败，约 37.3 秒，其中双启动约 34.3 秒。这是本机集成测试，不是公司登录性能测量。
- 首次该双启动测试总预算 90 秒超时并完成清理；调整到两个 Host 各自最多 90 秒所需的总预算 210 秒，同时传入测试取消信号后单独重跑通过。未增加生产 Host 的 90 秒启动超时。
- safeStorage 测试使用合成加密实现，shell 测试模拟 Electron 系统对象。真实 Windows OS 解密/跨重启、真实 IAM、安装目录和 app.asar 下动态导入/fork仍需实机验证。

原独立增量的 shell 测试只读复用共享 TypeScript；整合仓库使用 `desktop/node_modules/typescript`。原生测试默认相邻的系统对应 runtime，也可设 `DSH_R65_RUNTIME`。`refresh-patch.py` 从三份固定上游源码重新生成 patch，不创建完整 staging；`prepare-native-shell.py` 仍用于随后正式候选 staging。

## 构建整合

已冻结源文件与哈希见 `incremental-manifest.json`。对已通过上游来源验证、含 `.maimi-native-candidate.json` 的完整独立 staging 使用：

```powershell
python scripts/prepare-native-shell.py --stage-root <完整独立staging绝对路径> --receipt-directory <独立构建回执目录>
```

脚本从已固定的官方源码重新生成三个被修改文件（`apps/desktop/src/main.ts`、`apps/desktop/scripts/electron-builder-config.mjs`、`apps/desktop-host/src/index.ts`），覆盖 staging 同路径。两个 TS 接线文件复制到 `apps/desktop/src/`，其余 `src/` 文件复制到 `apps/desktop/maimi/`。不会修改上游 client bundle。指定回执目录可避免构建时改写本冻结目录的回执。脚本不构建、不启动应用、不发布。

## 与后续整合的关系

此设备续登证据产生时，企业本地桥尚未接好，因此 `validation.json` 的 cloud_entry_removal 保留当时的 WAITING 状态。后续已把实际 service 本地接线叠入同仓库：当前 profile 不再插入独立云端链接，仅授权员工主动启用 service；完整 Host/Peer/浏览器输入区与原生模型合成回归通过。其他企业项目技能的本机授权与公司业务验收仍待完成，不能用 service 单项通过放行全部功能。

原验证 JSON 作为历史证据不改写；新增接线与复测记录位于 `../tests/enterprise/`。签名更新/回滚、最终 Windows 安装器、完整 Office payload、真实业务和历史迁移验收不在本历史增量的通过范围。
