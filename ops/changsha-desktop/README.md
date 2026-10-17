# 长沙网页与桌面服务端 r11

完整配套为服务器r11、桌面/下载分发0.8.0、DSH 0.1.6-alpha.2。五镜像内容ID、最终文件名和SHA256以交付清单为准，旧r9/r10不包含本轮功能。公司此前仅确认r9镜像导入，新服务未启动。完整操作顺序见 [FIRST-PILOT.md](FIRST-PILOT.md)。

IAM首页 /maimi/ 提供打开网页版和下载Windows版。网页仅使用本人个人云工作区；桌面同时提供本机工作区。同员工两端共享云端聊天文件，独立登录/退出，打开另一端不替换运行Host。本机数据留员工电脑。每员工云Host同时运行一个任务，多端可以查看历史文件，尚非协同编辑。

## 身份与配置

正式IAM是 https://iam.megmeet.com，回调 https://platform-ai.megmeet.com/v1/iam/callback，应用首页 https://platform-ai.megmeet.com/maimi/。IAM_BASE_URL和IAM_AUTHORIZATION_BASE_URL必须一致，旧测试地址被校验器拒绝。正式IAM采用公司DNS，不继承测试域名10.4.241.103映射；TokenKey保留2026-09-20验证的10.4.1.112映射。实际正式DNS/TLS与新版容器出站待公司验证。

bash configure.sh 私下输入正式应用client_id/client_secret及TokenKey服务X-API-Key，不需要目录apiKey/apiSecret。IAM_IDENTITY_MODE=sso_tokenkey将服务器IAM profile.username与同一access_token获得的TokenKey username精确匹配，不信任浏览器工号，不推断部门权限。认证持续复查；退出端任务不会借用其他客户端凭据。

首次生成数据库密码、Fernet加密密钥和云调度密钥，已有密码/密钥保留。配置.env为0600，secret为root:10001/0440，容器UID10001只读。不进入桌面、镜像、发布包或聊天。遇.configuration-in-progress先检查部分写入，不能忽略；已有数据库卷但缺原配置时不会生成新密码覆盖。

## 统一启动与公开入口

公司既有Docker Nginx/common-net方式：

```bash
bash configure.sh --check --nginx-network
bash start-server.sh --nginx-network --distribution-dir /data/实际下载包目录
```

下载目录须包含已校验的start-distribution.sh。启动脚本校验源码、五个精确镜像、配置和下载包，再启动私有服务、迁移、下载站，并生成runtime-entry.locations.conf。维护已启动实例加--allow-running；独立连接器配置通过检查后另加--connectors。固定本机Docker socket及项目，不临时pull、不清空旧卷。

Gateway仅发布127.0.0.1:18091，下载站仅发布127.0.0.1:18092；数据库和Redis不映射宿主端口。Nginx容器通过megmeet-desktop-20260918-gateway:8080与megmeet-maimi-downloads:8080连接，不能使用容器自己的127.0.0.1。日期是稳定兼容别名，不代表镜像版本。

**启动服务不会自动切换公开入口。** 先备份现有platform-ai HTTPS server配置，再按实际内容替换与生成片段冲突的location，nginx -t成功后reload。用户已授权新系统接管旧/health等占位，旧项目数据保留。更早正则、^~前缀、server级rewrite/return仍要检查；不能仅追加片段就声称可用。Gateway重建后检查静态upstream解析并reload。详见FIRST-PILOT第4步。

完整入口包含IAM、桌面授权、个人模型/云、办公插件、/v1/connectors/、/maimi/web/、原生根资源/RPC、双端WebSocket和/maimi/下载。原样保留Cookie/Authorization/Origin，回调不缓存不记录查询；下载站剥离授权头及Cookie。新location关闭继承旧auth_request，由Gateway执行新身份校验。根/api/remote.mux的Upgrade不能省略。

## 数据、容量与维护

Gateway/迁移/清理共用r11Gateway，manager与个人DSH使用r11专用镜像；PostgreSQL16/Redis7使用清单固定离线标签。平台linux/amd64，启动前核对images-manifest.json内容ID。宿主需要Python3.10+、Compose v2、支持docker image inspect --platform linux/amd64的Docker，Engine支持范围包含API1.47。

个人云state/files卷绑定稳定员工身份；历史90天不活跃进入回收站、额外保留30天，普通项目文件不随聊天过期删除。旧本地/部门历史不自动迁移删除，详见[存储说明](../../docs/STORAGE-LIFECYCLE.md)。默认两个活跃Host，每个1536MiB/1CPU/128进程，空闲300秒回收进程保留卷；额外请求有界排队，不是两账号限制或全员并发承诺。

独立办公引擎保留Word/Excel/PPT/PDF/图片解析转换及离线OCR，运行资源随镜像提供；上游原生预览/文件对比与编辑转换并存。操作限当前获准附件与工作区，生成新文件保留原件、遵守DSH写权限。复杂排版和OCR结果需复核。

钉钉、搜索、浏览器操作通过独立连接器和当前对话选择使用，电脑操作仅桌面本机授权。图标全员可见不授予全员数据权限。真实钉钉应用、搜索服务和网络需独立配置，不复用IAM或模型TokenKey。写操作经用户确认，模型不能生成确认凭据。

本地已验证隔离合成IAM/TokenKey、真实Nginx、双端原生聊天文件、账号隔离及退出；正式IAM/TokenKey、HTTPS、连接器授权和容量待公司执行。不增加双账号人工验收前提。机外备份、卷硬额度、告警和Windows企业代码签名仍须落实，维护恢复见[MAINTENANCE.md](MAINTENANCE.md)。
