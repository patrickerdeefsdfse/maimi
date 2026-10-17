# 公司 r11 完整版部署

本次使用完整服务器 r11 包、桌面/下载分发 0.8.0 包，DSH 前端、运行时与 RPC 同步为 0.1.6-alpha.2。旧 r9/r10 包保留但不包含本轮功能；最终文件名、SHA256 和镜像内容 ID 以交付清单为准。公司此前仅确认 r9 五镜像已导入，新服务未启动。以下是已准备流程，不能记为公司已执行。

## 1. 准备完整包

经现有堡垒机/MobaXterm上传两个完整包和SHA256文件，校验后分别解压到新独立目录，不覆盖旧r9/r10，不删除旧项目/数据库/文件/卷。服务器包先运行 verify-package.py、只读 server-preflight.py，再按清单运行 load-images.sh；五镜像内容 ID 全部通过后才配置。

server-preflight.py --network 不发送凭据。正式 IAM 为 https://iam.megmeet.com，使用公司DNS，不套用旧测试IAM的IP；TokenKey保留已验证的10.4.1.112映射。正式IAM DNS/TLS和新版容器出站仍需公司执行验证。

## 2. 私下填写四项参数

进入服务器包 new-system/ops/changsha-desktop，运行 bash configure.sh，依次输入正式IAM地址、正式应用client_id、同应用client_secret、TokenKey服务X-API-Key。最后一项不是员工个人tk-密钥。不需要目录apiKey/apiSecret。

秘密输入不回显、不回传。首次自动生成数据库密码、Fernet会话加密密钥和云调度密钥；已有密码/加密密钥保留，无效配置阻止启动。切换正式IAM时必须重新输入正式应用凭据。

正式注册回调为 https://platform-ai.megmeet.com/v1/iam/callback；IAM工作台应用首页为 https://platform-ai.megmeet.com/maimi/。首页提供“打开网页版”和“下载 Windows 版”。身份由服务器将IAM profile.username与同一个access_token获得的TokenKey username精确匹配，不推断部门权限。

## 3. 启动服务与下载站

运行 bash configure.sh --check --nginx-network。公司已证实的 nginx-https 在 common-net，随后使用：

```bash
bash start-server.sh --nginx-network --distribution-dir /data/实际下载包目录
```

该路径是解压后包含 start-distribution.sh 的下载站目录。脚本验证服务器源码、配置、精确镜像和下载包，再启动服务、迁移与下载站，生成 runtime-entry.locations.conf，不打印展开的秘密配置。再次操作已启动实例加 --allow-running，只接受本项目精确占用18091。

真实连接器配置通过 check-connectors.py 后才另加 --connectors；钉钉需要独立应用凭据，不能复用IAM参数。没有可用配置时插件显示未连接。

## 4. 接入现有 HTTPS

启动容器不等于公开入口已完成，脚本不会静默改写Nginx。生成片段使用 megmeet-desktop-20260918-gateway:8080 与 megmeet-maimi-downloads:8080，日期是稳定兼容别名而非镜像版本。

实际入口是 nginx-https 中精确的 platform-ai.megmeet.com HTTPS server。现有证据只有过滤后的84路由摘要；必须先在服务器备份原配置，按实际内容替换冲突location，再合并 runtime-entry.locations.conf。先 nginx -t，通过后 reload，失败恢复备份；不要停止承载TLS的Nginx。

新入口包含 /maimi/、/maimi/web/、IAM/桌面授权/个人模型/个人云/办公插件/连接器API，以及 /api/、/plugins/、/assets/、/ext/、/__megmeet/、图标/manifest、/health、/ready。旧 /health 指向会议服务，需要替换。用户已授权新系统优先，旧项目数据仍保留。已有更早正则、^~ /api/、server级rewrite/return可能拦截新入口，不能仅追加片段。

根 /api/remote.mux 和桌面 /v1/personal-cloud/native/api/remote.mux 都需Upgrade。Cookie/Authorization/Origin原样转发，回调不缓存不记录查询；下载站剥离Cookie/Authorization。新路由不继承旧auth_request，由新Gateway执行IAM授权。

## 5. 员工端真实核对

核对正式IAM跳转、TokenKey身份/模型目录、多轮聊天、文件操作及HTTPS信任。网页/桌面共享本人云聊天文件、各自独立退出；本机资料仍在本机。同一员工云Host同时只运行一段任务，不提供协同编辑。

本轮隔离合成链已验证真实Nginx、Cookie/桌面授权、原生WebSocket、跨端聊天文件、B隔离和单端退出；不能代替正式公司IAM/TokenKey、钉钉/搜索授权、网络和容量联调。不增加双账号人工验收前提。

默认两个活跃Host，每个1536MiB/1CPU；额外请求有界排队，空闲300秒回收进程保留卷，不是两个账号限制或8GiB服务器全员并发承诺。维护备份见 [MAINTENANCE.md](MAINTENANCE.md)。
