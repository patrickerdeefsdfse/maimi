# 麦米办公助手下载站

这是独立的静态软件分发附加包。页面入口为 **https://platform-ai.megmeet.com/maimi/**，由正式 IAM 门户里的“麦米办公助手”应用链接打开，提供“开始网页聊天”和“下载桌面客户端”。正式 IAM 目标为 **https://iam.megmeet.com**；回调仍是 **https://platform-ai.megmeet.com/v1/iam/callback**。正式应用配置与真实账号联调仍需在公司环境完成。网页登录后只进入本人云端工作区，桌面端支持本人本机与云端工作区。

## 当前状态

本地源码和合成验证不表示公司域名已经上线。`site/release.json` 默认为 `unpublished`，按钮不可下载；主发布流程写入安装器、公开的签名更新清单和发布元数据后，才显示具体可下载版本。此目录不得放发布私钥、IAM 密钥、TokenKey、环境配置、服务端部署包或员工数据。

## 发布方文件契约

- `site/releases/<filename>.exe`：Windows x64 安装器，名称只允许 ASCII 字母、数字、点、短横、下划线，禁止 `..`，总长度最多155字符。使用带版本号的不可变文件名，不能覆盖同名版本内容。
- `site/updates/company-win-x64.json`：桌面更新器使用的签名公开清单，服务端原字节返回，不签名、不重写、不推测签名格式。主发布流程负责签名和离线校验。
- `site/release.json`：`{"schema_version":1,"status":"published","version":"0.8.0","filename":"MEGMEET-AI-Setup-0.8.0-x64.exe","size_bytes":123,"sha256":"安装器64位小写SHA256"}`。示例 size/hash 必须由实际产物计算，不能直接发布。
- `site/app-icon.png`：沿用桌面应用图标。

构建方完成以上文件后，运行 `python package.py --output /absolute/path/megmeet-desktop-distribution-0.8.0.zip`。默认拒绝未发布占位状态；只有制作页面预览包时才加 `--allow-unpublished`。打包仅复制固定允许清单，并生成逐文件大小/SHA256清单；不会打包本目录之外的内容。SHA256保证传输一致性，不能替代桌面更新签名或 Windows Authenticode。未取得企业代码签名证书时不能宣称安装器已受 Windows 签名信任。

## 公司部署

当前首次部署使用服务器r11包完成离线镜像加载，搭配下载分发0.8.0附加包；两包均兼容Python3.10宿主机校验。0.8.0安装向导允许员工选择路径，后台更新沿用该路径，正常退出后交接安装。下载站复用已固定内容的Gateway镜像运行一个独立、只读、无秘密的静态服务，不修改该镜像或运行中的Gateway。将本附加包上传到服务器的独立发布目录，先校验传输SHA256，再解压。保持目录对UID10001可读/可进入，文件可读，禁止给服务写权限。

进入解压后的 `desktop-distribution`：

```bash
bash start-distribution.sh --nginx-network
```

以上选项只适合现有 `nginx-https` 已加入 `common-net` 的情况。宿主机 Nginx 则省略该选项。脚本核对附加包和固定镜像内容，只管理 `megmeet-desktop-distribution` Compose 项目；不会下载依赖、停用旧业务、删除数据、修改 IAM 或重载 Nginx。

把 `nginx-maimi.locations.conf` 合入现有 `platform-ai.megmeet.com` HTTPS server 前，先确认没有同名 `/maimi` location。Docker Nginx 将片段的 `127.0.0.1:18092` **仅替换为** `megmeet-maimi-downloads:8080`；宿主机 Nginx 保留原值。代理不得增加末尾 `/`，以保留 `/maimi/` 路径。检查配置通过后再重载。下载容器重建后，使用静态解析名称的 Nginx 需重载以重新解析。仅新增这两条 `/maimi` location，保持域名根路径、所有 `/v1/` API 和 OAuth 回调不变。

验证下载页与 `/maimi/release.json`、版本 EXE 的 HEAD/Range，以及固定更新清单可读取。没有“必须两个账号人工验收”的前置限制。页面本身不承载 IAM 会话或员工信息；软件使用资格由实际桌面登录后的公司 IAM 授权决定。

## 访问与更新

服务绑定宿主机 `127.0.0.1:18092`，只通过既有 HTTPS 入口发布。HTTP 无目录浏览、上传接口、任意文件读取、其他 API 路由。支持单段 byte Range、HEAD、If-Range；拒绝多段/超长 Range。页面、发行元数据、更新清单使用 `no-store`；版本安装器可长期缓存。

更新页面/安装器时创建一个新的、已核验的发布目录，再从新目录运行相同项目名的启动脚本切换只读挂载；保持旧目录和旧版本安装器以便正在下载的客户端完成，别就地覆盖正在提供的 EXE。是否启用软件自动更新由桌面主进程验证签名清单后决定；本静态站不会执行安装程序。

下一步历史文件处理仅做只读盘点。下载站上线不依赖先删除 `/data` 历史；不能按目录名称猜测数据无用，也不要运行全局 prune 或删除持久卷。
