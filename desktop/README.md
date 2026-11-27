# 麦米 Windows 客户端 0.15.10

麦米办公助手通过企业 IAM 登录，在 Windows x64 工作区使用本机文件、Office 工具和已授权的插件。Electron 负责界面。插件宿主使用随包的 Node 子进程，员工插件及个人设置保存在用户目录中。

## 发行版本

| 组件 | 已核验的版本 |
| --- | --- |
| 麦米 Windows | 0.15.10，安装包 370,216,838 字节 |
| DeepSeek Harness | 0.2.2-rc.5，源码提交 `9c2670d3bce5275e0d1610272958121af87787bb` |
| Electron / 插件宿主 Node | 44.11.5 / 24.22.3 |
| pnpm / Windows Python | 11.28.5 / CPython 3.12 x64 |
| 本机办公与传输 | Pillow 13.0.0，MCP client 和 core 2.4.0，fflate 0.8.3 |
| 浏览器数学渲染 | KaTeX 0.18.10，Mermaid 11.17.2 |
| 网络依赖 | 随组件分别使用 undici 8.13.1、6.29.1。独立 Node 自带 undici 7.30.2。 |

[产品及上游版本](deps/pins.json)包括 Windows 安装程序、DSH、插件和 7-Zip 的摘要。[本次发行记录](deps/windows-release-0.15.10.json)列有程序包及前一版本的校验值。正式旧客户端已发现 0.15.10、完成差量升级并保留自定义安装目录和用户插件。安装器采用公司 HTTPS 源与下载完整性校验，本版没有企业 Authenticode 签名。

## 源码与构建输入

- `shell/src` 和 `shell/maimi` 实现桌面主进程、企业登录、插件分发和宿主启动。`shell/renderer` 保存窗口界面与实际发布的更新对话框样式。
- `runtime/plugins/managed/runtime` 收录随包的托管插件实现。其中 `policy.mjs` 内部版本表的 DSH 字段沿用了 rc.1。桌面运行清单和发布校验锁定 rc.5。
- `installer` 和 `scripts` 包含安装向导、升级、资源封装和安装器校验。构建工具位于 `scripts/package.json`，包括 electron-builder 26.17.0 与 tsdown 0.22.2。
- `scripts/release-0.15.10` 包含本次 Electron、Node、Pillow、浏览器依赖和 Windows 插件宿主的增量准备程序。浏览器构建脚本支持 Mermaid 与社区插件源码，详见[输入和输出摘要](deps/browser-assets-0.15.10.json)。
- 构建方需另行提供获授权使用的上一版 Windows 运行目录、原生 DSH Host、Office 运行时和图稿。旧版本的部署配置、安装包、签名材料、个人配置和员工授权清单由各部署方独立保管。仓库中的静态授权表为空，当次会话的授权仍由服务端决定。

本次发布从已经验证的 0.15.9 程序包增量生成。部分较早的 host、shell 和 runtime 依赖锁文件仍含历史构建版本，不足以在空白机器上还原当前完整的 Windows 运行时。公开整理的源码通过模块和渠道测试，未在全新 Windows 系统重新生成相同摘要的安装器。

## 设置站点和下载渠道

从本目录执行以下示例，给登录网关和网页入口各指定一个 HTTPS 来源。命令将可配置源码写入全新目录。构建期的地址同时用于登录授权页校验、企业工具、网页链接和更新来源校验。

```powershell
python scripts/configure-site.py ..\desktop-configured --gateway https://gateway.example.org --web https://web.example.org
Set-Location ..\desktop-configured
```

Windows 更新器从网关的 `/maimi/updates/windows/` 读取 `stable.yml`、`nightly.yml` 或兼容入口 `latest.yml`。更新程序只接受同一站点和目录下的版本化 x64 安装器及其 blockmap。门户网页上的手动下载分享地址由服务端单独设置。部署方应检查已展开程序中的 `resources/app-update.yml` 及已编译的主进程与新站点一致。

## 准备和打包

1. 在 Windows x64 准备 Python 3.12、Node 24、pnpm 11.28.5、7-Zip，以及编译安装进度窗口所需的 x86 Visual Studio C++ 工具。运行 `pnpm --dir scripts install --frozen-lockfile` 安装打包工具。
2. `MAIMI_PREVIOUS_WORK` 指向授权的 0.15.9 构建目录。其中包括 `windows/win-unpacked`、`validation/maimi-0.15.9.7z`、`installer-ui/window-frame.dll` 和 `source-assets` 中的两份 7-Zip 26.06 输入。将 `MAIMI_WORK` 指向同一卷上的独立新目录。增量准备程序使用硬链接，并在替换文件前校验基线。若 7-Zip 不在 PATH，可用 `MAIMI_7ZIP` 指定其程序路径。
3. `MAIMI_VENDOR_INPUTS` 指向本地供应目录。按[第三方输入摘要](deps/windows-vendors-0.15.10.json)中指定的文件名准备已锁定的发行包。九份经校验的浏览器构建输出放在其 `browser` 子目录，以各自的 SHA256 作为文件名。安装器使用构建方获授权的 `assets/installer/rail.bmp` 和 `header.bmp`，其他品牌资源按所用打包脚本补齐。
4. 下列命令在已适配所选站点的合格基线上准备本次组件增量。不同站点还须按固定上游版本重新编译 Windows 壳并提供对应的部署配置。未经配置的示例域名不能用于生成可分发安装器。

```powershell
$recipe = 'scripts/release-0.15.10'
python "$recipe/prepare.py" prepare
python "$recipe/prepare.py" extra
python "$recipe/update-node.py"
python "$recipe/patch-office.py"
python "$recipe/patch-host.py"
python "$recipe/apply-embedded.py" deps/browser-assets-0.15.10.json
node "$recipe/reseal.mjs" "$env:MAIMI_WORK/windows/win-unpacked/resources/app/dsh"
node scripts/build_installer.mjs
```

`build_installer.mjs` 校验已准备的应用、组件和下载源，将新安装包及更新元数据写入 `MAIMI_WORK/windows/release`。不同的站点、图稿、源依赖或压缩设置会改变最终文件摘要。原发行产物的 SHA256 为 `91c81eb2159eec95aca3a88b8601403054828fcfb2a544b6a4cec0f04361c19f`。

运行 `node --test tests/*.test.mjs` 可检查更新源、登录后的用户配置和连接器逻辑。提供已封装的 DWS 组件后可执行相应的集成测试。第三方组件与原许可文件一同使用。
